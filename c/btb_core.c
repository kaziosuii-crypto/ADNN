/*
 * BtB 神经元计算单元（C11，ctypes 可直接调用）。
 *
 * 边界（按用户要求的最小实现）：
 *   - C 里只放“神经元自身”：value、active、出边数组；
 *   - 每次激活 = 一条独立的 C 线程：线程内完成该神经元的 sigmoid、窗口登记、
 *     到期后的开火与传播，然后线程退出（thrd_detach，不复用、不池化）；
 *   - 只有一个最小计时线程负责把到期的窗口取出来执行开火；
 *   - 衰减不由 C 调度：Python 每秒调用一次 btb_decay_all()，C 只负责“把每条边 -0.1、
 *     删掉 <=0.1 的边”这一神经元动作；
 *   - 落盘、图帧、统计展示都由 Python 通过 export/read 接口按需读取。
 *
 * 线程数没有上限：每条激活就新建一条 C 线程，线程不受 GIL 限制。
 */

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#include <threads.h>

#ifdef _WIN32
#include <windows.h>
#define BTB_EXPORT __declspec(dllexport)
#else
#include <pthread.h>
#define BTB_EXPORT __attribute__((visibility("default")))
#endif

#define BTB_PI 3.14159265358979323846
#define STRIPES 64
#define SMALL_STACK (64u * 1024u)

typedef struct {
    int64_t target;
    double weight;
} Edge;

typedef struct Node {
    int64_t id;
    double value;
    unsigned char active;
    double fire_at;
    int32_t n_edges;
    int32_t cap_edges;
    Edge *edges;
    struct Node *next;
} Node;

typedef struct {
    double at;
    int64_t id;
} Timer;

typedef struct {
    int64_t id;
    double value;
    unsigned char active;
} Flip;

typedef struct Stripe {
    mtx_t mtx;
    Node **buckets;
    uint32_t nbuckets;
    int64_t n_nodes;
    uint64_t rng;
} Stripe;

typedef struct Engine {
    Stripe stripes[STRIPES];
    double window;
    double scale;
    int dps;
    int visual_size;
    atomic_int stop;
    atomic_llong node_count;
    atomic_llong active_count;
    atomic_llong edge_count;
    /* 在途激活线程数：销毁引擎前必须等到 0，避免线程还在访问已释放的内存。 */
    atomic_llong inflight;
    mtx_t dopa_mtx;
    double dopamine;
    /* 计时堆 + 计时线程 */
    mtx_t timer_mtx;
    cnd_t timer_cv;
    Timer *heap;
    uint32_t heap_n;
    uint32_t heap_cap;
    thrd_t timer_thread;
    int timer_started;
    /* 供 Python 采样的缓冲 */
    mtx_t evt_mtx;
    Flip *flips;
    uint32_t n_flips;
    uint32_t cap_flips;
    int64_t *fires;
    uint32_t n_fires;
    uint32_t cap_fires;
    int64_t *dirty;
    uint32_t n_dirty;
    uint32_t cap_dirty;
} Engine;

/* ---------- 时间 ---------- */

static double now_sec(void)
{
#ifdef _WIN32
    LARGE_INTEGER freq, counter;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
#endif
}

static void realtime_after(double seconds, struct timespec *ts)
{
#ifdef _WIN32
    struct timespec base;
    timespec_get(&base, TIME_UTC);
#else
    clock_gettime(CLOCK_REALTIME, &base);
#endif
    double target = (double)base.tv_sec + (double)base.tv_nsec * 1e-9 + seconds;
    ts->tv_sec = (time_t)target;
    ts->tv_nsec = (long)((target - (double)ts->tv_sec) * 1e9);
}

static uint32_t hash_id(int64_t id)
{
    uint64_t x = (uint64_t)id;
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    return (uint32_t)x;
}

static Stripe *stripe_of(Engine *e, int64_t id)
{
    return &e->stripes[hash_id(id) % STRIPES];
}

static double rng01(Stripe *s)
{
    s->rng ^= s->rng << 13;
    s->rng ^= s->rng >> 7;
    s->rng ^= s->rng << 17;
    return (double)(s->rng >> 11) * (1.0 / 9007199254740992.0);
}

/* ---------- 节点表（每条 stripe 一把锁，激活线程可并行） ---------- */

static Node *map_find(Stripe *s, int64_t id)
{
    uint32_t slot = hash_id(id) & (s->nbuckets - 1);
    for (Node *n = s->buckets[slot]; n; n = n->next) {
        if (n->id == id) return n;
    }
    return NULL;
}

static void map_grow(Stripe *s)
{
    uint32_t next = s->nbuckets * 2;
    Node **fresh = (Node **)calloc(next, sizeof(Node *));
    if (!fresh) return;
    for (uint32_t i = 0; i < s->nbuckets; ++i) {
        Node *n = s->buckets[i];
        while (n) {
            Node *following = n->next;
            uint32_t slot = hash_id(n->id) & (next - 1);
            n->next = fresh[slot];
            fresh[slot] = n;
            n = following;
        }
    }
    free(s->buckets);
    s->buckets = fresh;
    s->nbuckets = next;
}

/* 调用方必须持有 stripe 锁 */
static Node *map_get_or_create(Engine *e, Stripe *s, int64_t id)
{
    Node *n = map_find(s, id);
    if (n) return n;
    if ((uint64_t)s->n_nodes >= (uint64_t)s->nbuckets * 2) map_grow(s);
    n = (Node *)calloc(1, sizeof(Node));
    if (!n) return NULL;
    n->id = id;
    n->value = (double)id / e->scale;
    uint32_t slot = hash_id(id) & (s->nbuckets - 1);
    n->next = s->buckets[slot];
    s->buckets[slot] = n;
    s->n_nodes += 1;
    atomic_fetch_add(&e->node_count, 1);
    return n;
}

static void node_add_edge(Engine *e, Node *n, int64_t target, double weight)
{
    for (int32_t i = 0; i < n->n_edges; ++i) {
        if (n->edges[i].target == target) {
            n->edges[i].weight = weight;
            return;
        }
    }
    if (n->n_edges == n->cap_edges) {
        int32_t next = n->cap_edges ? n->cap_edges * 2 : 4;
        Edge *grown = (Edge *)realloc(n->edges, (size_t)next * sizeof(Edge));
        if (!grown) return;
        n->edges = grown;
        n->cap_edges = next;
    }
    n->edges[n->n_edges].target = target;
    n->edges[n->n_edges].weight = weight;
    n->n_edges += 1;
    atomic_fetch_add(&e->edge_count, 1);
}

/* ---------- 计时堆 ---------- */

static void heap_push(Engine *e, double at, int64_t id)
{
    if (e->heap_n == e->heap_cap) {
        uint32_t next = e->heap_cap ? e->heap_cap * 2 : 4096;
        Timer *grown = (Timer *)realloc(e->heap, (size_t)next * sizeof(Timer));
        if (!grown) return;
        e->heap = grown;
        e->heap_cap = next;
    }
    uint32_t i = e->heap_n++;
    e->heap[i].at = at;
    e->heap[i].id = id;
    while (i > 0) {
        uint32_t parent = (i - 1) / 2;
        if (e->heap[parent].at <= e->heap[i].at) break;
        Timer tmp = e->heap[parent];
        e->heap[parent] = e->heap[i];
        e->heap[i] = tmp;
        i = parent;
    }
}

static void heap_pop(Engine *e, Timer *out)
{
    *out = e->heap[0];
    e->heap_n -= 1;
    if (e->heap_n == 0) return;
    e->heap[0] = e->heap[e->heap_n];
    uint32_t i = 0;
    for (;;) {
        uint32_t left = i * 2 + 1;
        uint32_t right = left + 1;
        uint32_t best = i;
        if (left < e->heap_n && e->heap[left].at < e->heap[best].at) best = left;
        if (right < e->heap_n && e->heap[right].at < e->heap[best].at) best = right;
        if (best == i) break;
        Timer tmp = e->heap[best];
        e->heap[best] = e->heap[i];
        e->heap[i] = tmp;
        i = best;
    }
}

/* ---------- 采样缓冲 ---------- */

static void push_flip(Engine *e, int64_t id, double value, int active)
{
    mtx_lock(&e->evt_mtx);
    if (e->n_flips == e->cap_flips) {
        uint32_t next = e->cap_flips ? e->cap_flips * 2 : 4096;
        Flip *grown = (Flip *)realloc(e->flips, (size_t)next * sizeof(Flip));
        if (!grown) {
            mtx_unlock(&e->evt_mtx);
            return;
        }
        e->flips = grown;
        e->cap_flips = next;
    }
    e->flips[e->n_flips].id = id;
    e->flips[e->n_flips].value = value;
    e->flips[e->n_flips].active = (unsigned char)(active ? 1 : 0);
    e->n_flips += 1;
    mtx_unlock(&e->evt_mtx);
}

static void push_fire(Engine *e, int64_t id)
{
    mtx_lock(&e->evt_mtx);
    if (e->n_fires == e->cap_fires) {
        uint32_t next = e->cap_fires ? e->cap_fires * 2 : 1024;
        int64_t *grown = (int64_t *)realloc(e->fires, (size_t)next * sizeof(int64_t));
        if (!grown) {
            mtx_unlock(&e->evt_mtx);
            return;
        }
        e->fires = grown;
        e->cap_fires = next;
    }
    e->fires[e->n_fires++] = id;
    mtx_unlock(&e->evt_mtx);
}

static void push_dirty(Engine *e, int64_t id)
{
    mtx_lock(&e->evt_mtx);
    if (e->n_dirty == e->cap_dirty) {
        uint32_t next = e->cap_dirty ? e->cap_dirty * 2 : 2048;
        int64_t *grown = (int64_t *)realloc(e->dirty, (size_t)next * sizeof(int64_t));
        if (!grown) {
            mtx_unlock(&e->evt_mtx);
            return;
        }
        e->dirty = grown;
        e->cap_dirty = next;
    }
    e->dirty[e->n_dirty++] = id;
    mtx_unlock(&e->evt_mtx);
}

/* ---------- 线程创建（每次激活一条线程） ---------- */

typedef struct {
    Engine *engine;
    int64_t id;
    double value;
} ActivationJob;

static void activation_compute(Engine *e, int64_t id, double value);

static int activation_main(void *arg)
{
    ActivationJob *job = (ActivationJob *)arg;
    activation_compute(job->engine, job->id, job->value);
    atomic_fetch_sub(&job->engine->inflight, 1);
    free(job);
    return 0;
}

/** 起一条独立的 C 线程跑一次神经元激活；线程结束后自动回收。 */
static int spawn_activation(Engine *e, int64_t id, double value)
{
    ActivationJob *job = (ActivationJob *)malloc(sizeof(ActivationJob));
    if (!job) return 0;
    job->engine = e;
    job->id = id;
    job->value = value;
    atomic_fetch_add(&e->inflight, 1);
#ifdef _WIN32
    thrd_t thread;
    int started = thrd_create(&thread, activation_main, job);
    if (started != thrd_success) {
        atomic_fetch_sub(&e->inflight, 1);
        free(job);
        return 0;
    }
    thrd_detach(thread);
#else
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, SMALL_STACK);
    pthread_t thread;
    int started = pthread_create(&thread, &attr, (void *(*)(void *))activation_main, job);
    pthread_attr_destroy(&attr);
    if (started != 0) {
        atomic_fetch_sub(&e->inflight, 1);
        free(job);
        return 0;
    }
    pthread_detach(thread);
#endif
    return 1;
}

typedef struct {
    Engine *engine;
    int64_t *ids;
    double *values;
    unsigned char *bits;
    int base;
    int count;
    int grid;
} DispatchJob;

/** 派发线程：把一段激活逐条变成独立线程（避免单线程创建 4000 条线程成为瓶颈）。 */
static int dispatch_main(void *arg)
{
    DispatchJob *job = (DispatchJob *)arg;
    if (job->grid) {
        for (int i = 0; i < job->count; ++i) {
            spawn_activation(job->engine, (int64_t)(job->base + i), (double)job->bits[i]);
        }
    } else {
        for (int i = 0; i < job->count; ++i) {
            spawn_activation(job->engine, job->ids[i], job->values[i]);
        }
    }
    free(job->ids);
    free(job->values);
    free(job->bits);
    atomic_fetch_sub(&job->engine->inflight, 1);
    free(job);
    return 0;
}

/**
 * 把整段激活拆给多条派发线程，它们再各自起神经元线程。
 * 数据先复制到 C 自己的内存：Python 的 ctypes 缓冲在调用返回后就会释放。
 */
static int dispatch_range(Engine *e, const int64_t *ids, const double *values,
                          const unsigned char *bits, int count, int dispatch_threads, int grid)
{
    if (count <= 0) return 0;
    if (dispatch_threads > count) dispatch_threads = count;
    if (dispatch_threads < 1) dispatch_threads = 1;
    int chunk = (count + dispatch_threads - 1) / dispatch_threads;
    int started = 0;
    for (int base = 0; base < count; base += chunk) {
        int length = chunk;
        if (base + length > count) length = count - base;
        DispatchJob *job = (DispatchJob *)malloc(sizeof(DispatchJob));
        if (!job) break;
        job->engine = e;
        job->base = base;
        job->count = length;
        job->grid = grid;
        job->ids = NULL;
        job->values = NULL;
        job->bits = NULL;
        if (grid) {
            job->bits = (unsigned char *)malloc((size_t)length);
            if (!job->bits) {
                free(job);
                break;
            }
            memcpy(job->bits, bits + base, (size_t)length);
        } else {
            job->ids = (int64_t *)malloc((size_t)length * sizeof(int64_t));
            job->values = (double *)malloc((size_t)length * sizeof(double));
            if (!job->ids || !job->values) {
                free(job->ids);
                free(job->values);
                free(job);
                break;
            }
            memcpy(job->ids, ids + base, (size_t)length * sizeof(int64_t));
            memcpy(job->values, values + base, (size_t)length * sizeof(double));
        }
        atomic_fetch_add(&e->inflight, 1);
#ifdef _WIN32
        thrd_t thread;
        if (thrd_create(&thread, dispatch_main, job) != thrd_success) {
            atomic_fetch_sub(&e->inflight, 1);
            free(job->ids);
            free(job->values);
            free(job->bits);
            free(job);
            break;
        }
        thrd_detach(thread);
#else
        pthread_t thread;
        if (pthread_create(&thread, NULL, (void *(*)(void *))dispatch_main, job) != 0) {
            atomic_fetch_sub(&e->inflight, 1);
            free(job->ids);
            free(job->values);
            free(job->bits);
            free(job);
            break;
        }
        pthread_detach(thread);
#endif
        started += 1;
    }
    return started;
}

/* ---------- 神经元动作 ---------- */

static void activation_compute(Engine *e, int64_t id, double value)
{
    Stripe *s = stripe_of(e, id);
    mtx_lock(&s->mtx);
    Node *n = map_get_or_create(e, s, id);
    if (!n) {
        mtx_unlock(&s->mtx);
        return;
    }
    double merged = 1.0 / (1.0 + exp(-(n->value + value)));
    n->value = merged;
    if (!n->active) {
        n->active = 1;
        n->fire_at = now_sec() + e->window;
        mtx_lock(&e->timer_mtx);
        heap_push(e, n->fire_at, id);
        cnd_signal(&e->timer_cv);
        mtx_unlock(&e->timer_mtx);
        atomic_fetch_add(&e->active_count, 1);
        push_flip(e, id, merged, 1);
    }
    mtx_unlock(&s->mtx);
}

/** 窗口到期：开火（选目标、强化边）并向所有命中目标投递激活。 */
static void fire_node(Engine *e, int64_t id, double at)
{
    Stripe *s = stripe_of(e, id);
    Edge *copy = NULL;
    int32_t degree = 0;
    double value = 0.0;
    int64_t target = 0;

    mtx_lock(&s->mtx);
    Node *n = map_find(s, id);
    if (!n || !n->active || n->fire_at != at) {
        mtx_unlock(&s->mtx);
        return;
    }
    n->active = 0;
    n->fire_at = 0.0;
    value = n->value;
    atomic_fetch_sub(&e->active_count, 1);
    push_flip(e, id, value, 0);
    push_fire(e, id);

    target = (int64_t)(sin(BTB_PI * value) * e->scale);
    mtx_lock(&e->dopa_mtx);
    double dopamine = e->dopamine;
    mtx_unlock(&e->dopa_mtx);
    if (dopamine > 0.0) {
        target += (int64_t)((rng01(s) * 2.0 - 1.0) * sqrt(dopamine) * e->scale);
    }

    int existing = 0;
    for (int32_t i = 0; i < n->n_edges; ++i) {
        if (n->edges[i].target == target) {
            n->edges[i].weight += 1.0;
            existing = 1;
            break;
        }
    }
    if (!existing) node_add_edge(e, n, target, 1.0);
    push_dirty(e, id);

    if (n->n_edges > 0) {
        copy = (Edge *)malloc((size_t)n->n_edges * sizeof(Edge));
        if (copy) {
            memcpy(copy, n->edges, (size_t)n->n_edges * sizeof(Edge));
            degree = n->n_edges;
        }
    }
    mtx_unlock(&s->mtx);

    for (int32_t i = 0; i < degree; ++i) {
        double gate = copy[i].weight;
        if (gate > 1.0) gate = 1.0;
        if (rng01(s) < gate) {
            spawn_activation(e, copy[i].target, value);
        }
    }
    free(copy);
}

static int timer_main(void *arg)
{
    Engine *e = (Engine *)arg;
    while (!atomic_load(&e->stop)) {
        mtx_lock(&e->timer_mtx);
        if (e->heap_n == 0) {
            struct timespec ts;
            realtime_after(0.05, &ts);
            cnd_timedwait(&e->timer_cv, &e->timer_mtx, &ts);
            mtx_unlock(&e->timer_mtx);
            continue;
        }
        double wait = e->heap[0].at - now_sec();
        if (wait > 0.0) {
            struct timespec ts;
            realtime_after(wait > 0.05 ? 0.05 : wait, &ts);
            cnd_timedwait(&e->timer_cv, &e->timer_mtx, &ts);
            mtx_unlock(&e->timer_mtx);
            continue;
        }
        Timer timer;
        heap_pop(e, &timer);
        mtx_unlock(&e->timer_mtx);
        fire_node(e, timer.id, timer.at);
    }
    return 0;
}

/* ---------- 对外 API ---------- */

BTB_EXPORT void *btb_create(double window, int dps, int visual_size)
{
    Engine *e = (Engine *)calloc(1, sizeof(Engine));
    if (!e) return NULL;
    e->window = window;
    e->dps = dps;
    e->scale = pow(10.0, (double)dps);
    e->visual_size = visual_size;
    atomic_init(&e->stop, 0);
    atomic_init(&e->node_count, 0);
    atomic_init(&e->active_count, 0);
    atomic_init(&e->edge_count, 0);
    atomic_init(&e->inflight, 0);
    mtx_init(&e->dopa_mtx, mtx_plain);
    mtx_init(&e->timer_mtx, mtx_plain);
    cnd_init(&e->timer_cv);
    mtx_init(&e->evt_mtx, mtx_plain);
    for (int i = 0; i < STRIPES; ++i) {
        Stripe *s = &e->stripes[i];
        mtx_init(&s->mtx, mtx_plain);
        s->nbuckets = 1024;
        s->buckets = (Node **)calloc(s->nbuckets, sizeof(Node *));
        s->rng = 0x9E3779B97F4A7C15ULL ^ ((uint64_t)(i + 1) * 0xBF58476D1CE4E5B9ULL);
        if (!s->buckets) return e;
    }
    e->timer_started = 1;
    thrd_create(&e->timer_thread, timer_main, e);
    return e;
}

BTB_EXPORT void btb_destroy(void *handle)
{
    Engine *e = (Engine *)handle;
    if (!e) return;
    atomic_store(&e->stop, 1);
    mtx_lock(&e->timer_mtx);
    cnd_broadcast(&e->timer_cv);
    mtx_unlock(&e->timer_mtx);
    if (e->timer_started) thrd_join(e->timer_thread, NULL);
    /* 等在途激活线程全部退出，再释放节点表。 */
    for (int guard = 0; guard < 5000 && atomic_load(&e->inflight) > 0; ++guard) {
        struct timespec pause = {0, 1000000};
        thrd_sleep(&pause, NULL);
    }
    for (int i = 0; i < STRIPES; ++i) {
        Stripe *s = &e->stripes[i];
        for (uint32_t b = 0; b < s->nbuckets; ++b) {
            Node *n = s->buckets[b];
            while (n) {
                Node *following = n->next;
                free(n->edges);
                free(n);
                n = following;
            }
        }
        free(s->buckets);
        mtx_destroy(&s->mtx);
    }
    free(e->heap);
    free(e->flips);
    free(e->fires);
    free(e->dirty);
    cnd_destroy(&e->timer_cv);
    mtx_destroy(&e->timer_mtx);
    mtx_destroy(&e->evt_mtx);
    mtx_destroy(&e->dopa_mtx);
    free(e);
}

BTB_EXPORT int btb_set_node(void *handle, int64_t id, double value)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    Stripe *s = stripe_of(e, id);
    mtx_lock(&s->mtx);
    Node *n = map_get_or_create(e, s, id);
    if (n) n->value = value;
    mtx_unlock(&s->mtx);
    return n ? 1 : 0;
}

BTB_EXPORT int btb_add_edge(void *handle, int64_t src, int64_t dst, double weight)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    Stripe *s = stripe_of(e, src);
    mtx_lock(&s->mtx);
    Node *n = map_get_or_create(e, s, src);
    if (n) node_add_edge(e, n, dst, weight);
    mtx_unlock(&s->mtx);
    return n ? 1 : 0;
}

BTB_EXPORT int btb_activate_many(void *handle, const int64_t *ids, const double *values, int count)
{
    Engine *e = (Engine *)handle;
    if (!e || !ids || !values || count <= 0) return 0;
    return dispatch_range(e, ids, values, NULL, count, 16, 0);
}

BTB_EXPORT int btb_activate_grid(void *handle, const unsigned char *bits, int count)
{
    Engine *e = (Engine *)handle;
    if (!e || !bits || count <= 0) return 0;
    if (e->visual_size > 0 && count > e->visual_size) count = e->visual_size;
    return dispatch_range(e, NULL, NULL, bits, count, 32, 1);
}

/** Python 每秒调用一次：把每条边权重 -0.1，删掉 <=0.1 的边。 */
BTB_EXPORT void btb_decay_all(void *handle)
{
    Engine *e = (Engine *)handle;
    if (!e) return;
    for (int i = 0; i < STRIPES; ++i) {
        Stripe *s = &e->stripes[i];
        mtx_lock(&s->mtx);
        for (uint32_t b = 0; b < s->nbuckets; ++b) {
            for (Node *n = s->buckets[b]; n; n = n->next) {
                if (n->n_edges == 0) continue;
                int32_t kept = 0;
                int removed = 0;
                for (int32_t k = 0; k < n->n_edges; ++k) {
                    double weight = n->edges[k].weight - 0.1;
                    if (weight > 0.1) {
                        n->edges[kept] = n->edges[k];
                        n->edges[kept].weight = weight;
                        kept += 1;
                    } else {
                        removed += 1;
                    }
                }
                n->n_edges = kept;
                if (removed) {
                    atomic_fetch_sub(&e->edge_count, removed);
                    push_dirty(e, n->id);
                }
            }
        }
        mtx_unlock(&s->mtx);
    }
}

BTB_EXPORT void btb_set_dopamine(void *handle, double value)
{
    Engine *e = (Engine *)handle;
    if (!e) return;
    mtx_lock(&e->dopa_mtx);
    e->dopamine = value;
    mtx_unlock(&e->dopa_mtx);
}

BTB_EXPORT double btb_get_dopamine(void *handle)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0.0;
    mtx_lock(&e->dopa_mtx);
    double value = e->dopamine;
    mtx_unlock(&e->dopa_mtx);
    return value;
}

BTB_EXPORT void btb_add_dopamine(void *handle, double delta)
{
    Engine *e = (Engine *)handle;
    if (!e) return;
    mtx_lock(&e->dopa_mtx);
    e->dopamine += delta;
    mtx_unlock(&e->dopa_mtx);
}

BTB_EXPORT void btb_stats(void *handle, long long *nodes, long long *active, long long *edges)
{
    Engine *e = (Engine *)handle;
    if (!e) return;
    if (nodes) *nodes = (long long)atomic_load(&e->node_count);
    if (active) *active = (long long)atomic_load(&e->active_count);
    if (edges) *edges = (long long)atomic_load(&e->edge_count);
}

BTB_EXPORT int btb_drain_flips(void *handle, int64_t *ids, double *values,
                               unsigned char *actives, int max, int *out_count)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    mtx_lock(&e->evt_mtx);
    int written = (int)e->n_flips;
    if (written > max) written = max;
    for (int i = 0; i < written; ++i) {
        if (ids) ids[i] = e->flips[i].id;
        if (values) values[i] = e->flips[i].value;
        if (actives) actives[i] = e->flips[i].active;
    }
    e->n_flips = 0;
    mtx_unlock(&e->evt_mtx);
    if (out_count) *out_count = written;
    return written;
}

BTB_EXPORT int btb_drain_fires(void *handle, int64_t *ids, int max, int *out_count)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    mtx_lock(&e->evt_mtx);
    int written = (int)e->n_fires;
    if (written > max) written = max;
    for (int i = 0; i < written; ++i) ids[i] = e->fires[i];
    e->n_fires = 0;
    mtx_unlock(&e->evt_mtx);
    if (out_count) *out_count = written;
    return written;
}

BTB_EXPORT int btb_drain_dirty(void *handle, int64_t *ids, int max, int *out_count)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    mtx_lock(&e->evt_mtx);
    int written = (int)e->n_dirty;
    if (written > max) written = max;
    for (int i = 0; i < written; ++i) ids[i] = e->dirty[i];
    e->n_dirty = 0;
    mtx_unlock(&e->evt_mtx);
    if (out_count) *out_count = written;
    return written;
}

BTB_EXPORT int btb_node(void *handle, int64_t id, double *value, unsigned char *active)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    Stripe *s = stripe_of(e, id);
    mtx_lock(&s->mtx);
    Node *n = map_find(s, id);
    if (n) {
        if (value) *value = n->value;
        if (active) *active = n->active;
    }
    mtx_unlock(&s->mtx);
    return n ? 1 : 0;
}

BTB_EXPORT int btb_edges(void *handle, int64_t id, int64_t *targets, double *weights,
                         int max, int *out_count)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    Stripe *s = stripe_of(e, id);
    int written = 0;
    mtx_lock(&s->mtx);
    Node *n = map_find(s, id);
    if (n) {
        for (int32_t i = 0; i < n->n_edges && written < max; ++i) {
            if (targets) targets[written] = n->edges[i].target;
            if (weights) weights[written] = n->edges[i].weight;
            written += 1;
        }
    }
    mtx_unlock(&s->mtx);
    if (out_count) *out_count = written;
    return written;
}

BTB_EXPORT int btb_seed_dirty(void *handle)
{
    Engine *e = (Engine *)handle;
    if (!e) return 0;
    int total = 0;
    for (int i = 0; i < STRIPES; ++i) {
        Stripe *s = &e->stripes[i];
        mtx_lock(&s->mtx);
        for (uint32_t b = 0; b < s->nbuckets; ++b) {
            for (Node *n = s->buckets[b]; n; n = n->next) push_dirty(e, n->id);
        }
        total += (int)s->n_nodes;
        mtx_unlock(&s->mtx);
    }
    return total;
}

BTB_EXPORT int btb_export_all(void *handle, int64_t *ids, double *values, unsigned char *actives,
                              int max_nodes, int64_t *edge_src, int64_t *edge_dst,
                              double *edge_w, int max_edges, int *out_edges)
{
    Engine *e = (Engine *)handle;
    if (!e || !ids || max_nodes <= 0) return 0;
    int nodes = 0;
    int edges = 0;
    for (int i = 0; i < STRIPES; ++i) {
        Stripe *s = &e->stripes[i];
        mtx_lock(&s->mtx);
        for (uint32_t b = 0; b < s->nbuckets && nodes < max_nodes; ++b) {
            for (Node *n = s->buckets[b]; n && nodes < max_nodes; n = n->next) {
                ids[nodes] = n->id;
                if (values) values[nodes] = n->value;
                if (actives) actives[nodes] = n->active;
                nodes += 1;
                for (int32_t k = 0; k < n->n_edges && edges < max_edges; ++k) {
                    if (edge_src) edge_src[edges] = n->id;
                    if (edge_dst) edge_dst[edges] = n->edges[k].target;
                    if (edge_w) edge_w[edges] = n->edges[k].weight;
                    edges += 1;
                }
            }
        }
        mtx_unlock(&s->mtx);
    }
    if (out_edges) *out_edges = edges;
    return nodes;
}
