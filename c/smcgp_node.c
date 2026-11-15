/*
 * SMCGP14 神经元 + 群体调度 + 多线程执行的 C 实现（ctypes 可直接调用）。
 *
 * 与 compute/node.py 完全一致的边界：
 *   - 14 种指令：INP/OUTPUT/DADD/DMULT/CONST/IFLTE/INDX + ADD/DEL/MOV/CHC/CHF/FLR/CHP；
 *   - 相对连接 c >= 1 只引用前方节点，越界/前向/自引用一律读作 0；
 *   - 一轮内“读取旧图、轮末按登记顺序提交修改”，只有 OUTPUT 追溯到的节点才执行；
 *   - 自修改区间左闭右开、越界夹紧；ADD 插入数量 = 16·sin(触发值) 取绝对值
 *     四舍五入后夹到 1..16（对任意实数都能映射到 1~16 个节点，不再由 p1 随机决定），
 *     且单神经元基因总数上限 = 2.5 × 10^dps（dps=5 -> 250000 个内部节点）；
 *     CHC 用 p1 % 2 选连接，CHF 用 p1 % 14，CHP 用 p1 % 3。
 *
 * 群体层只做最小调度：每个节点一份待处理输入表，一轮结束后按路由规则
 * （目标编码 × 10^dps 后对 10^dps 取模）把 emission 汇总到目标节点的下一轮输入。
 * 一轮内的节点求值由常驻 C 线程池并行执行（池大小 = smcgp_set_workers）；
 * 图结构落盘、JSON、回调派发全部留在 Python。
 */

#include <math.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <threads.h>
#include <time.h>

#if defined(_WIN32)
#define SM_EXPORT __declspec(dllexport)
#else
#define SM_EXPORT __attribute__((visibility("default")))
#endif

#define SM_OP_COUNT 14
/* ADD 单轮插入数量的上界 = 16·sin(x) 的值域上界（映射后夹到 1..16）。 */
#define SM_MAX_ADD_GENES 16
/* 控制台统计用的“保留区间”上限：视觉 / 操作感觉四组 / 刺激神经元等固定段。 */
#define SM_MAX_RESERVED 8
/* 全局内部节点（基因）总数上限：800 万。任何神经元的 ADD 生长都不能把它顶破。 */
#define SM_MAX_TOTAL_GENES 8000000
#define SM_RANDOM_RANGE 16
#define SM_DEFAULT_BUCKETS (1u << 16)
/* 路由分片：把“把 emission 写进目标节点的下一轮输入”按目标 ID 散到多把锁上，
 * 线程先把本轮发射按分片排好，再每片只抢一次锁，避免十万级发射逐条抢锁。 */
#define SM_ROUTE_SHARDS 128

typedef struct Gene {
    int op;
    int c0;
    int c1;
    double p0, p1, p2;
} Gene;

typedef struct Mod {
    int index;
    int op;
    double p0, p1, p2;
    /* 触发值 a（本轮该节点算出的输入）：ADD 用它经 16·sin(x) 决定插入数量。 */
    double trigger;
} Mod;

typedef struct Emission {
    double value;
    double destination;
} Emission;

typedef struct Node {
    int64_t id;
    Gene *genes;
    int gene_count;
    int gene_cap;
    int inp_total;   /* 本轮 INP 数量（每轮现算，供 INP 读取槽位时使用） */
    uint64_t rng;
    /* 本轮求值暂存（长度按 gene_cap 扩容）；同一时刻只有执行它的那条线程会碰 */
    double *values;
    uint8_t *marks;
    int *slots;
    int *estack;
    int estack_cap;
    /* 图结构缓存：INP 槽位与 OUTPUT 位置只在图变化后重建，
     * 否则每轮都要扫一遍基因数组（巨型图单节点可达二十万基因，扫一遍就是毫秒级）。 */
    int *outputs;
    int output_count;
    int outputs_cap;
    int cache_valid;
    int graph_dirty;   /* 图结构变过：下一轮执行前必须重建缓存 */
    /* 上一轮被求值触碰过的下标：只清这些 marks，避免整图 memset。 */
    int *touched;
    int touched_count;
    int touched_cap;
    /* 本轮输入 / 下一轮输入 */
    double *inbuf;
    int in_count;
    int in_cap;
    double *nextbuf;
    int next_count;
    int next_cap;
    int in_dirty;
    int in_next;
    int modified;    /* 图结构自上次落盘后是否改变过 */
    struct Node *next;
} Node;

/* 每条执行线程自己的暂存：发射缓冲、修改队列、INP 分配槽、路由/登记缓冲。 */
typedef struct RouteItem {
    double value;
    int64_t target;   /* 外层寻址后的目标 ID（node_run 时算好，合并时只看整数） */
    int shard;        /* target % SM_ROUTE_SHARDS */
} RouteItem;

typedef struct Worker {
    struct Engine *engine;
    int index;
    Emission *emits;
    int emit_count;
    int emit_cap;
    Mod *mods;
    int mod_count;
    int mod_cap;
    double *inslots;
    int inslots_cap;
    /* 轮末一次性合并到引擎的登记（避免每个节点/每条消息都抢全局锁） */
    RouteItem *routes;
    int route_count;
    int route_cap;
    RouteItem *sorted;              /* 按分片排好序的发射缓冲 */
    int sorted_cap;
    int shard_counts[SM_ROUTE_SHARDS];
    int shard_offsets[SM_ROUTE_SHARDS];
    int64_t *fired;
    int fired_count;
    int fired_cap;
    Node **mod_nodes;
    int mod_node_count;
    int mod_node_cap;
    int last_job;           /* 本线程最后参与的轮次序号：防止同一轮重复收尾把 finished 加多 */
    /* BTB_DEBUG=1 时：最近一个节点的三段耗时（重建缓存 / 求值 / 自修改） */
    long long dbg_rebuild_ns;
    long long dbg_eval_ns;
    long long dbg_commit_ns;
} Worker;

/* 路由分片结构：每片一把锁 + 本轮该片的待执行节点登记。 */
typedef struct Shard {
    mtx_t lock;
    Node **next;        /* 本分片内下一轮要执行的节点（去重） */
    int next_count;
    int next_cap;
} Shard;

typedef struct Engine {
    Node **buckets;
    uint32_t bucket_count;
    mtx_t mtx;              /* 保护节点表、待处理列表、发射/修改登记、统计追加 */
    int dps;
    int64_t scale;
    /* 单神经元基因（内部节点）数量上限 = 2.5 × 10^dps。
     * 只在生长（ADD 插入）时拦截：已存在的超大神经元保持原图，只是不再增长。 */
    int64_t max_genes;
    /* 全局内部节点（基因）总数上限（默认 800 万）：所有神经元共享这一份预算。 */
    int64_t max_total_genes;
    /* 诊断：累计被基因上限拦下的 ADD 次数（给 BTB_DEBUG 的轮次日志用）。 */
    atomic_llong dbg_cap_hits;
    /* 拒收路由输入的区间 [block_base, block_base+block_size)：用于刺激神经元等
     * “只接受显式激活、不接受别的神经元消息”的节点（size=0 表示不限制）。 */
    int64_t block_base;
    int64_t block_size;
    /* 控制台统计：保留区间（视觉 / 四组 / 刺激神经元）之外才算“扩展神经元”。
     * 区间由 Python 侧通过 smcgp_set_reserved_ranges 下发，C 只做范围判断。 */
    int64_t reserved_start[SM_MAX_RESERVED];
    int64_t reserved_end[SM_MAX_RESERVED];
    int reserved_count;
    /* 上一轮统计：执行总数 / 其中扩展神经元数；下一轮待执行总数 / 其中扩展神经元数。 */
    atomic_llong stats_run_total;
    atomic_llong stats_run_expanded;
    atomic_llong stats_pending_total;
    atomic_llong stats_pending_expanded;
    uint64_t base_seed;
    Node **dirty;
    int dirty_count;
    int dirty_cap;
    Shard *shards;          /* 路由分片（锁 + 本轮待执行登记） */
    int shard_count;
    int64_t *fired;
    int fired_count;
    int fired_cap;
    Node **mod_nodes;       /* 自上次 take_modified 以来图结构改过的节点 */
    int mod_node_count;
    int mod_node_cap;
    atomic_llong node_count;
    atomic_llong gene_total;
    int last_error;
    /* 线程池 */
    int worker_count;       /* 线程数；0 表示主线程串行执行 */
    thrd_t *threads;
    Worker *workers;        /* 至少 1 个（串行时主线程用 workers[0]） */
    mtx_t job_mtx;
    cnd_t job_cv;
    cnd_t done_cv;
    Node **run_nodes;       /* 本轮要执行的节点（指向 dirty 数组） */
    int run_count;
    int batch;              /* 动态取任务时每次领取的节点数（兼顾负载均衡与原子争用） */
    atomic_int next_index;  /* 下一条线程要去领取的起始下标 */
    int job_seq;            /* 轮次序号：每条线程每轮只允许参与一次 */
    int workers_expected;   /* 本轮需要完成收尾的线程数 */
    int finished;
    int job_active;
    int stopping;
    /* BTB_DEBUG=1 时的轮次诊断（诊断输出必须走 stderr，stdout 是 bridge 的纯 JSON） */
    int debug_log;
    atomic_llong round_seq;
    atomic_llong round_emissions;
    atomic_llong dbg_node_ns;       /* 本轮所有节点求值累计耗时（各线程相加） */
    atomic_llong dbg_eval_ns;       /* 其中：OUTPUT 求值部分 */
    atomic_llong dbg_commit_ns;     /* 其中：自修改提交（含基因数组搬移） */
    atomic_llong dbg_slowest_ns;    /* 本轮最慢的单个节点耗时 */
    atomic_llong dbg_slowest_id;
    atomic_llong dbg_slowest_genes;
    atomic_llong dbg_slowest_rebuild_ns;
    atomic_llong dbg_slowest_eval_ns;
    atomic_llong dbg_slowest_commit_ns;
} Engine;

/* ---------- 通用工具 ---------- */

/* 单调时钟秒数：只用于 BTB_DEBUG=1 时的轮次耗时诊断。 */
static double mono_seconds(void)
{
    struct timespec ts;
#if defined(_WIN32)
    timespec_get(&ts, TIME_UTC);
#else
    clock_gettime(CLOCK_MONOTONIC, &ts);
#endif
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static int clamp_int(int value, int low, int high)
{
    if (high < low) return low;
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

static uint64_t mix_seed(uint64_t base, int64_t id)
{
    uint64_t x = base ^ ((uint64_t)id * 0x9E3779B97F4A7C15ULL);
    x ^= x >> 30;
    x *= 0xBF58476D1CE4E5B9ULL;
    x ^= x >> 27;
    x *= 0x94D049BB133111EBULL;
    x ^= x >> 31;
    return x ? x : 0x9E3779B97F4A7C15ULL;
}

static uint64_t rnd_next(Node *n)
{
    uint64_t x = n->rng;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    n->rng = x;
    return x * 2685821657736338717ULL;
}

static double rnd_double(Node *n)
{
    return (double)(rnd_next(n) >> 11) * (1.0 / 9007199254740992.0);
}

static int rnd_int(Node *n, int low, int high)
{
    if (high <= low) return low;
    uint64_t span = (uint64_t)(high - low + 1);
    return low + (int)(rnd_next(n) % span);
}

static double rnd_uniform(Node *n, double low, double high)
{
    return low + rnd_double(n) * (high - low);
}

static int grow_bytes(void **buffer, int *capacity, int need, size_t item_size)
{
    if (need <= *capacity) return 1;
    int cap = *capacity > 0 ? *capacity : 8;
    while (cap < need) cap *= 2;
    void *fresh = realloc(*buffer, (size_t)cap * item_size);
    if (!fresh) return 0;
    *buffer = fresh;
    *capacity = cap;
    return 1;
}

/* ---------- 内部节点表的扩容与查找 ---------- */

/* 一次性扩容某节点的全部求值暂存（值缓存、状态标记、INP 槽位、显式求值栈、
 * OUTPUT 缓存、触碰列表）。重新分配的 marks 必须清零，否则旧数据会污染求值。 */
static int scratch_ensure(Node *n, int need)
{
    if (need <= 0) need = 1;
    int stack_cap = need * 2 + 64;
    if (n->values == NULL || n->estack_cap < stack_cap) {
        double *values = realloc(n->values, (size_t)need * sizeof(double));
        if (!values) return 0;
        n->values = values;
        uint8_t *marks = realloc(n->marks, (size_t)need * sizeof(uint8_t));
        if (!marks) return 0;
        n->marks = marks;
        memset(n->marks, 0, (size_t)need * sizeof(uint8_t));
        int *slots = realloc(n->slots, (size_t)need * sizeof(int));
        if (!slots) return 0;
        n->slots = slots;
        int *estack = realloc(n->estack, (size_t)stack_cap * sizeof(int));
        if (!estack) return 0;
        n->estack = estack;
        n->estack_cap = stack_cap;
    }
    /* 触碰列表最多等于基因数：一次扩够，求值热路径里就不再反复 grow。 */
    if (n->touched_cap < need) {
        int *touched = realloc(n->touched, (size_t)need * sizeof(int));
        if (!touched) return 0;
        n->touched = touched;
        n->touched_cap = need;
    }
    return 1;
}

static int genome_ensure(Node *n, int need)
{
    if (need <= n->gene_cap) return 1;
    int cap = n->gene_cap > 0 ? n->gene_cap : 8;
    while (cap < need) cap *= 2;
    Gene *genes = realloc(n->genes, (size_t)cap * sizeof(Gene));
    if (!genes) return 0;
    n->genes = genes;
    n->gene_cap = cap;
    return scratch_ensure(n, cap);
}

static Node *engine_find(Engine *e, int64_t id)
{
    uint32_t bucket = (uint32_t)((uint64_t)id % e->bucket_count);
    for (Node *n = e->buckets[bucket]; n != NULL; n = n->next) {
        if (n->id == id) return n;
    }
    return NULL;
}

static Gene random_gene(Node *n);
static int push_node(Node ***array, int *count, int *cap, Node *node);
static void engine_mark_modified(Engine *e, Node *n);

/* 登记“图结构已改变”的节点；调用方必须已持有引擎锁。 */
static void mark_modified_locked(Engine *e, Node *n)
{
    n->graph_dirty = 1;
    if (n->modified) return;
    n->modified = 1;
    if (!push_node(&e->mod_nodes, &e->mod_node_count, &e->mod_node_cap, n)) {
        e->last_error = 1;
    }
}

/* 取节点，不存在时按“随机种子图”创建；调用方必须已持有引擎锁。
 * 随机种子图 = INP + 1~4 个随机节点 + 一个随机回连的 OUTPUT。 */
static Node *node_get_or_create(Engine *e, int64_t id)
{
    Node *n = engine_find(e, id);
    if (n != NULL) return n;
    n = (Node *)calloc(1, sizeof(Node));
    if (n == NULL) {
        e->last_error = 1;
        return NULL;
    }
    n->id = id;
    n->rng = mix_seed(e->base_seed, id);
    int middle = rnd_int(n, 1, 4);
    int count = middle + 2;
    if (!genome_ensure(n, count)) {
        free(n);
        e->last_error = 1;
        return NULL;
    }
    n->genes[0].op = 0; /* INP */
    n->genes[0].c0 = 1;
    n->genes[0].c1 = 1;
    n->genes[0].p0 = n->genes[0].p1 = n->genes[0].p2 = 0.0;
    for (int i = 1; i <= middle; ++i) {
        n->genes[i] = random_gene(n);
    }
    Gene *output = &n->genes[count - 1];
    output->op = 1; /* OUTPUT：两个连接都指向前方，随机决定目标编码 */
    output->c0 = rnd_int(n, 1, count - 1);
    output->c1 = rnd_int(n, 1, count - 1);
    output->p0 = output->p1 = output->p2 = 0.0;
    n->gene_count = count;
    uint32_t bucket = (uint32_t)((uint64_t)id % e->bucket_count);
    n->next = e->buckets[bucket];
    e->buckets[bucket] = n;
    atomic_fetch_add(&e->node_count, 1);
    atomic_fetch_add(&e->gene_total, count);
    mark_modified_locked(e, n);
    return n;
}

/* ---------- 输入缓冲与登记列表 ---------- */

static int append_input(double **buffer, int *count, int *cap, double value)
{
    if (!grow_bytes((void **)buffer, cap, *count + 1, sizeof(double))) return 0;
    (*buffer)[(*count)++] = value;
    return 1;
}

static int push_node(Node ***array, int *count, int *cap, Node *node)
{
    if (!grow_bytes((void **)array, cap, *count + 1, sizeof(Node *))) return 0;
    (*array)[(*count)++] = node;
    return 1;
}

static int engine_push_dirty(Engine *e, Node *n)
{
    if (n->in_dirty) return 1;
    if (!push_node(&e->dirty, &e->dirty_count, &e->dirty_cap, n)) return 0;
    n->in_dirty = 1;
    return 1;
}

/* 无锁读取节点表：桶数组大小固定、节点只增不删、链表只在头部插入，
 * 所以读线程只要用宽松原子读链指针就能安全遍历（写线程已持全局锁）。 */
static Node *sm_load_ptr(Node *const *slot)
{
#if defined(_MSC_VER)
    return *slot;
#else
    return __atomic_load_n(slot, __ATOMIC_RELAXED);
#endif
}

static Node *node_lookup_relaxed(Engine *e, int64_t id)
{
    uint32_t bucket = (uint32_t)((uint64_t)id % e->bucket_count);
    Node *n = sm_load_ptr(&e->buckets[bucket]);
    while (n != NULL) {
        if (n->id == id) return n;
        n = sm_load_ptr(&n->next);
    }
    return NULL;
}

static int engine_push_fired(Engine *e, int64_t id)
{
    if (!grow_bytes((void **)&e->fired, &e->fired_cap, e->fired_count + 1, sizeof(int64_t))) return 0;
    e->fired[e->fired_count++] = id;
    return 1;
}

/* 标记某节点图结构已改变；同一节点在一次落盘前只登记一次。 */
static void engine_mark_modified(Engine *e, Node *n)
{
    mtx_lock(&e->mtx);
    mark_modified_locked(e, n);
    mtx_unlock(&e->mtx);
}

/* ---------- 线程本地登记（轮末统一合并，避免每条消息抢全局锁） ---------- */

/* 外层寻址：目标编码 × 10^dps 后对 10^dps 取模，覆盖整个路由 ID 空间；
 * 顺便算出目标落在哪个分片。 */
static void route_target(Engine *e, double destination, int64_t *out_target, int *out_shard)
{
    double scaled = fabs(destination) * (double)e->scale;
    if (!isfinite(scaled)) scaled = 0.0;
    scaled = fmod(scaled, (double)e->scale);
    int64_t target = (int64_t)scaled;
    if (target < 0) target = 0;
    if (target >= e->scale) target = e->scale - 1;
    *out_target = target;
    *out_shard = (int)((uint64_t)target % (uint64_t)e->shard_count);
}

static int worker_push_route(Worker *w, double value, double destination)
{
    if (!grow_bytes((void **)&w->routes, &w->route_cap, w->route_count + 1, sizeof(RouteItem))) return 0;
    RouteItem *item = &w->routes[w->route_count++];
    item->value = value;
    route_target(w->engine, destination, &item->target, &item->shard);
    Engine *e = w->engine;
    if (e->block_size > 0
            && item->target >= e->block_base
            && item->target < e->block_base + e->block_size) {
        /* 该目标拒收路由输入（刺激神经元只接受显式激活）：直接丢弃这条 emission。 */
        w->route_count -= 1;
        return 1;
    }
    return 1;
}

static int worker_push_fired_id(Worker *w, int64_t id)
{
    if (!grow_bytes((void **)&w->fired, &w->fired_cap, w->fired_count + 1, sizeof(int64_t))) return 0;
    w->fired[w->fired_count++] = id;
    return 1;
}

static void worker_mark_modified(Worker *w, Node *n)
{
    /* 图结构变了：下一轮执行前必须重建 INP 槽位 / OUTPUT 位置缓存。 */
    n->graph_dirty = 1;
    if (n->modified) return;
    n->modified = 1;
    if (!push_node(&w->mod_nodes, &w->mod_node_count, &w->mod_node_cap, n)) {
        w->engine->last_error = 1;
    }
}

/* ---------- 图结构操作 ---------- */

static Gene random_gene(Node *n)
{
    Gene gene;
    gene.op = rnd_int(n, 0, SM_OP_COUNT - 1);
    gene.c0 = rnd_int(n, 1, SM_RANDOM_RANGE);
    gene.c1 = rnd_int(n, 1, SM_RANDOM_RANGE);
    gene.p0 = rnd_uniform(n, -SM_RANDOM_RANGE, SM_RANDOM_RANGE);
    gene.p1 = rnd_uniform(n, -SM_RANDOM_RANGE, SM_RANDOM_RANGE);
    gene.p2 = rnd_uniform(n, -SM_RANDOM_RANGE, SM_RANDOM_RANGE);
    return gene;
}

/* 全局基因预算的原子申领：最多申领 want 个，返回实际拿到的数量（0 表示已达 800 万上限）。
 * 用 CAS 预占，避免多条线程同时把总数顶过上限。 */
static int claim_total_genes(Engine *e, int want)
{
    if (want <= 0) return 0;
    int64_t prev = atomic_load(&e->gene_total);
    for (;;) {
        int64_t room = e->max_total_genes - prev;
        if (room <= 0) return 0;
        int take = (int64_t)want < room ? want : (int)room;
        if (atomic_compare_exchange_strong(&e->gene_total, &prev, prev + take)) return take;
        /* CAS 失败时 prev 已被更新为最新值，直接重试。 */
    }
}

static int genome_insert_random(Engine *e, Worker *w, Node *n, int position, int count)
{
    if (count <= 0) return 0;
    /* 单神经元基因上限 2.5 × 10^dps：只允许长到上限，超出部分直接丢弃。
     * 计算方式与 Python 版 Node(max_genes=...) 完全一致。 */
    int64_t room = e->max_genes - (int64_t)n->gene_count;
    if (room <= 0) {
        atomic_fetch_add(&e->dbg_cap_hits, 1);
        return 0;
    }
    if ((int64_t)count > room) {
        count = (int)room;
        atomic_fetch_add(&e->dbg_cap_hits, 1);
    }
    if (!genome_ensure(n, n->gene_count + count)) return 0;
    /* 全局 800 万基因上限：原子预占实际可用额度，拿不到就放弃这次插入。 */
    int granted = claim_total_genes(e, count);
    if (granted <= 0) {
        atomic_fetch_add(&e->dbg_cap_hits, 1);
        return 0;
    }
    count = granted;
    memmove(n->genes + position + count, n->genes + position,
            (size_t)(n->gene_count - position) * sizeof(Gene));
    for (int i = 0; i < count; ++i) {
        n->genes[position + i] = random_gene(n);
    }
    n->gene_count += count;
    worker_mark_modified(w, n);
    return 1;
}

static void genome_delete(Engine *e, Worker *w, Node *n, int start, int end)
{
    if (end <= start) return;
    memmove(n->genes + start, n->genes + end, (size_t)(n->gene_count - end) * sizeof(Gene));
    int removed = end - start;
    n->gene_count -= removed;
    atomic_fetch_sub(&e->gene_total, removed);
    worker_mark_modified(w, n);
}

/* ---------- 求值 ---------- */

static double read_link(Node *n, int index, int link)
{
    if (link < 1) return 0.0;
    int dep = index - link;
    if (dep < 0 || dep >= n->gene_count) return 0.0;
    return n->marks[dep] == 2 ? n->values[dep] : 0.0;
}

static int has_two_inputs(int op)
{
    /* DADD=2, DMULT=3, IFLTE=5, OUTPUT=1 以及 7 种自修改指令 */
    return op == 1 || op == 2 || op == 3 || op == 5 || (op >= 7 && op <= 13);
}

static void distribute_inputs(const double *values, int total, int count, double *out)
{
    if (count <= 0) return;
    if (total <= 0) {
        memset(out, 0, (size_t)count * sizeof(double));
        return;
    }
    if (count <= total) {
        for (int slot = 0; slot < count; ++slot) {
            long low = (long)((double)total * slot / count);
            long high = ((long)total * (slot + 1) + count - 1) / count;
            if (high <= low) high = low + 1;
            if (high > total) high = total;
            double sum = 0.0;
            int span = (int)(high - low);
            for (long i = low; i < high; ++i) sum += values[i];
            out[slot] = span > 0 ? sum / span : 0.0;
        }
        return;
    }
    /* 槽位比输入多：环形补齐。用取模改成环形游标，十万级槽位时省掉每次取模的除法。 */
    int cursor = 0;
    for (int slot = 0; slot < count; ++slot) {
        out[slot] = values[cursor];
        if (++cursor >= total) cursor = 0;
    }
}

static int worker_push_mod(Worker *w, int index, int op, double p0, double p1, double p2,
                           double trigger)
{
    if (!grow_bytes((void **)&w->mods, &w->mod_cap, w->mod_count + 1, sizeof(Mod))) return 0;
    Mod *item = &w->mods[w->mod_count++];
    item->index = index;
    item->op = op;
    item->p0 = p0;
    item->p1 = p1;
    item->p2 = p2;
    item->trigger = trigger;
    return 1;
}

static int worker_push_emit(Worker *w, double value, double destination)
{
    if (!grow_bytes((void **)&w->emits, &w->emit_cap, w->emit_count + 1, sizeof(Emission))) return 0;
    w->emits[w->emit_count].value = value;
    w->emits[w->emit_count].destination = destination;
    w->emit_count += 1;
    return 1;
}

static double compute_value(Engine *e, Worker *w, Node *n, int index)
{
    Gene *gene = &n->genes[index];
    int op = gene->op;
    if (op == 0) { /* INP */
        int slot = n->slots[index];
        if (slot < 0 || slot >= n->inp_total) return 0.0;
        return w->inslots[slot];
    }
    if (op == 4) return gene->p0; /* CONST */
    if (op == 6) return (double)index; /* INDX */
    double a = read_link(n, index, gene->c0);
    double b = read_link(n, index, gene->c1);
    switch (op) {
    case 2: return a + b; /* DADD */
    case 3: return a * b; /* DMULT */
    case 5: return a < 0.0 ? b : 0.0; /* IFLTE */
    case 1: return a; /* OUTPUT */
    default: break;
    }
    if (op >= 7 && op <= 13) { /* 自修改指令：a >= b 触发并登记，否则返回 b */
        if (a >= b) {
            worker_push_mod(w, index, op, gene->p0, gene->p1, gene->p2, a);
            return a;
        }
        return b;
    }
    (void)e;
    return 0.0;
}

static double eval_index(Engine *e, Worker *w, Node *n, int index)
{
    if (index < 0 || index >= n->gene_count) return 0.0;
    if (n->marks[index] == 2) return n->values[index];
    int sp = 0;
    n->estack[sp++] = index;
    n->estack[sp++] = 0;
    while (sp > 0) {
        int stage = n->estack[--sp];
        int current = n->estack[--sp];
        if (current < 0 || current >= n->gene_count) continue;
        if (stage == 0) {
            if (n->marks[current] == 2) continue;
            if (n->marks[current] == 1) {
                /* 环：直接把该节点压成 0 */
                n->marks[current] = 2;
                n->values[current] = 0.0;
                continue;
            }
            if (!grow_bytes((void **)&n->estack, &n->estack_cap, sp + 6, sizeof(int))) {
                e->last_error = 1;
                break;
            }
            if (n->touched_count >= n->touched_cap) {
                if (!grow_bytes((void **)&n->touched, &n->touched_cap, n->touched_count + 1,
                                sizeof(int))) {
                    e->last_error = 1;
                    break;
                }
            }
            n->touched[n->touched_count++] = current;
            n->marks[current] = 1;
            n->estack[sp++] = current;
            n->estack[sp++] = 1;
            Gene *gene = &n->genes[current];
            if (has_two_inputs(gene->op)) {
                int links[2] = { gene->c0, gene->c1 };
                for (int k = 0; k < 2; ++k) {
                    if (links[k] < 1) continue;
                    int dep = current - links[k];
                    if (dep < 0 || dep >= n->gene_count) continue;
                    if (n->marks[dep] != 0) continue;
                    n->estack[sp++] = dep;
                    n->estack[sp++] = 0;
                }
            }
            continue;
        }
        n->marks[current] = 2;
        n->values[current] = compute_value(e, w, n, current);
    }
    return n->marks[index] == 2 ? n->values[index] : 0.0;
}

/* ---------- 自修改提交 ---------- */

/* ADD 插入数量：16·sin(x) 把所有实数映射到 [-16,16]，取绝对值后四舍五入，
 * 再夹到 1..SM_MAX_ADD_GENES（=16）。取整用 floor(|y| + 0.5)，与 Python 版完全一致。 */
static int add_count_from_value(double trigger)
{
    double scaled = 16.0 * sin(trigger);
    if (!isfinite(scaled)) scaled = 0.0;
    double rounded = floor(fabs(scaled) + 0.5);
    int count = (int)rounded;
    if (count < 1) count = 1;
    if (count > SM_MAX_ADD_GENES) count = SM_MAX_ADD_GENES;
    return count;
}

static int apply_mod(Engine *e, Worker *w, Node *n, const Mod *mod)
{
    int size = n->gene_count;
    int index = mod->index;
    int op = mod->op;
    if (op == 7) { /* ADD */
        int start = clamp_int(index + (int)mod->p0, 0, size);
        /* 数量不再由 p1 决定：16·sin(触发值 a) 映射到 1..16 个节点。 */
        int count = add_count_from_value(mod->trigger);
        return genome_insert_random(e, w, n, start, count);
    }
    if (op == 8) { /* DEL */
        int start = clamp_int(index + (int)mod->p0, 0, size);
        int end = clamp_int(start + (int)mod->p1, start, size);
        if (end <= start) return 0;
        genome_delete(e, w, n, start, end);
        return 1;
    }
    if (op == 9) { /* MOV */
        int start = clamp_int(index + (int)mod->p0, 0, size);
        int length = clamp_int((int)mod->p1, 0, size - start);
        if (length <= 0) return 0;
        Gene *block = (Gene *)malloc((size_t)length * sizeof(Gene));
        if (!block) return 0;
        memcpy(block, n->genes + start, (size_t)length * sizeof(Gene));
        memmove(n->genes + start, n->genes + start + length,
                (size_t)(n->gene_count - start - length) * sizeof(Gene));
        n->gene_count -= length;
        int target = clamp_int(index + (int)mod->p2, 0, n->gene_count);
        if (!genome_ensure(n, n->gene_count + length)) {
            free(block);
            return 0;
        }
        memmove(n->genes + target + length, n->genes + target,
                (size_t)(n->gene_count - target) * sizeof(Gene));
        memcpy(n->genes + target, block, (size_t)length * sizeof(Gene));
        n->gene_count += length;
        free(block);
        worker_mark_modified(w, n);
        return 1;
    }
    if (op == 10) { /* CHC */
        int target = clamp_int(index + (int)mod->p0, 0, size - 1);
        if (size <= 0) return 0;
        int link = (int)mod->p2;
        if (link < 1) link = 1;
        if (((int)mod->p1) % 2 == 0) n->genes[target].c0 = link;
        else n->genes[target].c1 = link;
        worker_mark_modified(w, n);
        return 1;
    }
    if (op == 11) { /* CHF */
        int target = clamp_int(index + (int)mod->p0, 0, size - 1);
        if (size <= 0) return 0;
        int value = ((int)mod->p1) % SM_OP_COUNT;
        if (value < 0) value += SM_OP_COUNT;
        n->genes[target].op = value;
        worker_mark_modified(w, n);
        return 1;
    }
    if (op == 13) { /* CHP */
        int target = clamp_int(index + (int)mod->p0, 0, size - 1);
        if (size <= 0) return 0;
        int which = ((int)mod->p1) % 3;
        if (which < 0) which += 3;
        double value = mod->p2;
        if (which == 0) n->genes[target].p0 = value;
        else if (which == 1) n->genes[target].p1 = value;
        else n->genes[target].p2 = value;
        worker_mark_modified(w, n);
        return 1;
    }
    return 0;
}

static void commit_mods(Engine *e, Worker *w, Node *n)
{
    int position = 0;
    while (position < w->mod_count) {
        Mod mod = w->mods[position++];
        if (mod.op == 12) { /* FLR：作废其后尚未提交的请求，本身不改图 */
            w->mod_count = 0;
            return;
        }
        apply_mod(e, w, n, &mod);
    }
    w->mod_count = 0;
}

/* ---------- 路由：把本轮 emission 写进目标节点的下一轮输入 ---------- */

/* 把某线程的发射按分片做计数排序后，每片只抢一次锁批量写入。
 * 单轮发射可达十万级，逐条抢锁会把合并段变成串行瓶颈；按片批量后锁操作从 O(发射)
 * 降到 O(线程 × 分片)。 */
static void worker_flush_routes(Engine *e, Worker *w)
{
    if (w->route_count <= 0) return;
    int shards = e->shard_count;
    if (!grow_bytes((void **)&w->sorted, &w->sorted_cap, w->route_count, sizeof(RouteItem))) {
        e->last_error = 1;
        w->route_count = 0;
        return;
    }
    memset(w->shard_counts, 0, sizeof(int) * (size_t)shards);
    for (int i = 0; i < w->route_count; ++i) w->shard_counts[w->routes[i].shard] += 1;
    int offset = 0;
    for (int s = 0; s < shards; ++s) {
        w->shard_offsets[s] = offset;
        offset += w->shard_counts[s];
    }
    for (int s = 0; s < shards; ++s) w->shard_counts[s] = w->shard_offsets[s];
    for (int i = 0; i < w->route_count; ++i) {
        RouteItem item = w->routes[i];
        w->sorted[w->shard_counts[item.shard]++] = item;
    }
    /* 每片批量写入：先无锁查目标，缺节点时才去抢一次全局锁。 */
    for (int s = 0; s < shards; ++s) {
        int start = w->shard_offsets[s];
        int count = w->shard_counts[s] - start;
        if (count <= 0) continue;
        Shard *shard = &e->shards[s];
        mtx_lock(&shard->lock);
        for (int k = 0; k < count; ++k) {
            RouteItem *item = &w->sorted[start + k];
            Node *to = node_lookup_relaxed(e, item->target);
            if (to == NULL) {
                mtx_unlock(&shard->lock);
                mtx_lock(&e->mtx);
                to = node_get_or_create(e, item->target);
                mtx_unlock(&e->mtx);
                mtx_lock(&shard->lock);
                if (to == NULL) continue;
            }
            if (append_input(&to->nextbuf, &to->next_count, &to->next_cap, item->value)) {
                if (!to->in_next) {
                    if (push_node(&shard->next, &shard->next_count, &shard->next_cap, to)) {
                        to->in_next = 1;
                    } else {
                        e->last_error = 1;
                    }
                }
            }
        }
        mtx_unlock(&shard->lock);
    }
}

/* 把某线程本轮累计的登记一次性合并进引擎（发射/修改抢一次全局锁，路由按分片批量写入）。 */
static void worker_flush_registers(Engine *e, Worker *w)
{
    if (w->fired_count == 0 && w->route_count == 0 && w->mod_node_count == 0) return;
    if (e->debug_log && w->route_count > 0) {
        atomic_fetch_add(&e->round_emissions, w->route_count);
    }
    mtx_lock(&e->mtx);
    for (int i = 0; i < w->fired_count; ++i) engine_push_fired(e, w->fired[i]);
    for (int i = 0; i < w->mod_node_count; ++i) mark_modified_locked(e, w->mod_nodes[i]);
    mtx_unlock(&e->mtx);
    worker_flush_routes(e, w);
    w->fired_count = 0;
    w->route_count = 0;
    w->mod_node_count = 0;
}

/* ---------- 单个神经元的一轮执行 ---------- */

static void node_run_impl(Engine *e, Worker *w, Node *n)
{
    if (n->gene_count <= 0) return;
    if (!scratch_ensure(n, n->gene_count)) {
        e->last_error = 1;
        return;
    }
    /* 1. INP 槽位与 OUTPUT 位置只在图变化后重建（巨型图每轮全扫一遍代价极大）。 */
    double rebuild_started = e->debug_log ? mono_seconds() : 0.0;
    if (!n->cache_valid || n->graph_dirty) {
        int slot = 0;
        int output_count = 0;
        if (!grow_bytes((void **)&n->outputs, &n->outputs_cap, n->gene_count, sizeof(int))) {
            e->last_error = 1;
            return;
        }
        memset(n->slots, 0xFF, (size_t)n->gene_count * sizeof(int));
        for (int i = 0; i < n->gene_count; ++i) {
            int op = n->genes[i].op;
            if (op == 0) n->slots[i] = slot++;
            else if (op == 1) n->outputs[output_count++] = i;
        }
        n->inp_total = slot;
        n->output_count = output_count;
        n->cache_valid = 1;
        n->graph_dirty = 0;
    }
    if (e->debug_log) {
        w->dbg_rebuild_ns = (long long)((mono_seconds() - rebuild_started) * 1e9);
    }
    /* 2. 输入分配 */
    if (n->inp_total > 0) {
        if (!grow_bytes((void **)&w->inslots, &w->inslots_cap, n->inp_total, sizeof(double))) {
            e->last_error = 1;
            return;
        }
        distribute_inputs(n->inbuf, n->in_count, n->inp_total, w->inslots);
    }
    /* 3. 清理本轮暂存：marks 只在上一轮触碰过的下标上清（避免整图 memset）。 */
    w->emit_count = 0;
    w->mod_count = 0;
    /* 4. 只有 OUTPUT 驱动计算 */
    double eval_started = e->debug_log ? mono_seconds() : 0.0;
    int produced = 0;
    for (int k = 0; k < n->output_count; ++k) {
        int i = n->outputs[k];
        Gene *gene = &n->genes[i];
        double value = gene->c0 >= 1 ? eval_index(e, w, n, i - gene->c0) : 0.0;
        double destination = gene->c1 >= 1 ? eval_index(e, w, n, i - gene->c1) : 0.0;
        if (worker_push_emit(w, value, destination)) produced += 1;
    }
    /* 求值结束：把本轮触碰过的 marks 全部清掉，供下一轮使用。 */
    for (int i = 0; i < n->touched_count; ++i) n->marks[n->touched[i]] = 0;
    n->touched_count = 0;
    if (e->debug_log) {
        w->dbg_eval_ns = (long long)((mono_seconds() - eval_started) * 1e9);
        atomic_fetch_add(&e->dbg_eval_ns, w->dbg_eval_ns);
    }
    /* 5. 轮末提交自修改 */
    double commit_started = e->debug_log ? mono_seconds() : 0.0;
    commit_mods(e, w, n);
    if (e->debug_log) {
        w->dbg_commit_ns = (long long)((mono_seconds() - commit_started) * 1e9);
        atomic_fetch_add(&e->dbg_commit_ns, w->dbg_commit_ns);
    }
    /* 6. 本节点产生消息：记入线程本地登记（轮末统一合并，避免每节点抢锁） */
    if (produced > 0) {
        worker_push_fired_id(w, n->id);
        for (int i = 0; i < w->emit_count; ++i) {
            worker_push_route(w, w->emits[i].value, w->emits[i].destination);
        }
    }
    /* 7. 本轮输入已消费 */
    n->in_count = 0;
}

/* 节点执行入口：BTB_DEBUG=1 时统计单节点耗时，用来定位“少数巨型图拖住整轮”的问题。 */
static void node_run(Engine *e, Worker *w, Node *n)
{
    if (!e->debug_log) {
        node_run_impl(e, w, n);
        return;
    }
    double started = mono_seconds();
    node_run_impl(e, w, n);
    long long ns = (long long)((mono_seconds() - started) * 1e9);
    atomic_fetch_add(&e->dbg_node_ns, ns);
    long long previous = atomic_load(&e->dbg_slowest_ns);
    while (ns > previous) {
        if (atomic_compare_exchange_weak(&e->dbg_slowest_ns, &previous, ns)) {
            atomic_store(&e->dbg_slowest_id, n->id);
            atomic_store(&e->dbg_slowest_genes, n->gene_count);
            atomic_store(&e->dbg_slowest_rebuild_ns, w->dbg_rebuild_ns);
            atomic_store(&e->dbg_slowest_eval_ns, w->dbg_eval_ns);
            atomic_store(&e->dbg_slowest_commit_ns, w->dbg_commit_ns);
            break;
        }
    }
}

/* ---------- 线程池 ---------- */

static int worker_main(void *arg)
{
    Worker *w = (Worker *)arg;
    Engine *e = w->engine;
    mtx_lock(&e->job_mtx);
    for (;;) {
        while (!e->job_active && !e->stopping) {
            cnd_wait(&e->job_cv, &e->job_mtx);
        }
        if (e->stopping) break;
        if (w->last_job == e->job_seq) {
            /* 本轮已经收尾过：绝不能再自增 finished，否则主线程会提前认为整轮结束，
             * 把它人还没合并完的发射/路由丢掉（表现为“路由没发生、待执行列表为空”）。 */
            cnd_wait(&e->job_cv, &e->job_mtx);
            continue;
        }
        w->last_job = e->job_seq;
        int batch = e->batch;
        int run_count = e->run_count;
        Node **run_nodes = e->run_nodes;
        mtx_unlock(&e->job_mtx);
        /* 动态领取：每次原子取走 batch 个节点，避免某个线程分到全是巨型图的区间而拖住整轮。 */
        for (;;) {
            int start = atomic_fetch_add(&e->next_index, batch);
            if (start >= run_count) break;
            int end = start + batch;
            if (end > run_count) end = run_count;
            for (int i = start; i < end; ++i) {
                Node *node = run_nodes[i];
                node->in_dirty = 0; /* 先清标记，否则下一轮 activate 会误以为还排在队列里 */
                node_run(e, w, node);
            }
        }
        worker_flush_registers(e, w);
        mtx_lock(&e->job_mtx);
        e->finished += 1;
        if (e->finished >= e->workers_expected) {
            e->job_active = 0;
            cnd_broadcast(&e->done_cv);
            cnd_broadcast(&e->job_cv);
        }
    }
    mtx_unlock(&e->job_mtx);
    return 0;
}

/* ---------- 对外接口 ---------- */

SM_EXPORT void *smcgp_create(int dps)
{
    Engine *e = (Engine *)calloc(1, sizeof(Engine));
    if (e == NULL) return NULL;
    e->bucket_count = SM_DEFAULT_BUCKETS;
    e->buckets = (Node **)calloc(e->bucket_count, sizeof(Node *));
    if (e->buckets == NULL) {
        free(e);
        return NULL;
    }
    if (mtx_init(&e->mtx, mtx_plain) != thrd_success
            || mtx_init(&e->job_mtx, mtx_plain) != thrd_success) {
        free(e->buckets);
        free(e);
        return NULL;
    }
    cnd_init(&e->job_cv);
    cnd_init(&e->done_cv);
    /* 路由分片：每片一把锁，避免十万级发射挤在一个互斥量上。 */
    e->shard_count = SM_ROUTE_SHARDS;
    e->shards = (Shard *)calloc((size_t)e->shard_count, sizeof(Shard));
    if (e->shards == NULL) {
        free(e->buckets);
        free(e);
        return NULL;
    }
    for (int i = 0; i < e->shard_count; ++i) {
        if (mtx_init(&e->shards[i].lock, mtx_plain) != thrd_success) {
            for (int j = 0; j < i; ++j) mtx_destroy(&e->shards[j].lock);
            free(e->shards);
            free(e->buckets);
            free(e);
            return NULL;
        }
    }
    if (dps < 1) dps = 1;
    if (dps > 9) dps = 9;
    e->dps = dps;
    e->scale = 1;
    for (int i = 0; i < dps; ++i) e->scale *= 10;
    /* 单神经元基因上限 = 2.5 × 10^dps（dps=5 -> 250000），与 nodeManager 的公式一致。 */
    double gene_limit = 2.5 * (double)e->scale;
    if (gene_limit > 2147483647.0) gene_limit = 2147483647.0; /* gene_count 是 int */
    e->max_genes = (int64_t)gene_limit;
    if (e->max_genes < 1) e->max_genes = 1;
    /* 全局内部节点（基因）总数上限：默认 800 万。 */
    e->max_total_genes = SM_MAX_TOTAL_GENES;
    e->base_seed = 0x2545F4914F6CDD1DULL;
    /* 只在 BTB_DEBUG=1 时打印轮次耗时诊断（走 stderr，不污染 bridge 的 stdout JSON）。 */
    const char *debug_env = getenv("BTB_DEBUG");
    e->debug_log = debug_env != NULL && debug_env[0] == '1';
    /* 串行回退用的 worker[0] */
    e->workers = (Worker *)calloc(1, sizeof(Worker));
    if (e->workers == NULL) {
        free(e->buckets);
        free(e);
        return NULL;
    }
    e->workers[0].engine = e;
    e->worker_count = 0;
    return e;
}

/* 设置执行线程数：必须在开始跑轮之前调用（Python 初始化时调用一次）。 */
SM_EXPORT void smcgp_set_workers(void *handle, int count)
{
    Engine *e = (Engine *)handle;
    if (e == NULL || count <= 0) return;
    if (e->worker_count > 0) return; /* 已经建过线程池 */
    if (count > 256) count = 256;
    Worker *workers = (Worker *)calloc((size_t)count, sizeof(Worker));
    thrd_t *threads = (thrd_t *)calloc((size_t)count, sizeof(thrd_t));
    if (workers == NULL || threads == NULL) {
        free(workers);
        free(threads);
        return;
    }
    for (int i = 0; i < count; ++i) {
        workers[i].engine = e;
        workers[i].index = i;
        /* 新线程把“已参与轮次”对齐到当前序号，避免插进正在进行的轮次里重复收尾。 */
        workers[i].last_job = e->job_seq;
    }
    int created = 0;
    for (int i = 0; i < count; ++i) {
        if (thrd_create(&threads[i], worker_main, &workers[i]) != thrd_success) break;
        created += 1;
    }
    if (created <= 0) {
        free(workers);
        free(threads);
        return;
    }
    free(e->workers[0].emits);
    free(e->workers[0].mods);
    free(e->workers[0].inslots);
    free(e->workers[0].routes);
    free(e->workers[0].sorted);
    free(e->workers[0].fired);
    free(e->workers[0].mod_nodes);
    free(e->workers);
    e->workers = workers;
    e->threads = threads;
    e->worker_count = created;
}

SM_EXPORT void smcgp_destroy(void *handle)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    if (e->worker_count > 0) {
        mtx_lock(&e->job_mtx);
        e->stopping = 1;
        cnd_broadcast(&e->job_cv);
        cnd_broadcast(&e->done_cv);
        mtx_unlock(&e->job_mtx);
        for (int i = 0; i < e->worker_count; ++i) {
            thrd_join(e->threads[i], NULL);
        }
        free(e->threads);
    }
    for (int i = 0; i < (e->worker_count > 0 ? e->worker_count : 1); ++i) {
        free(e->workers[i].emits);
        free(e->workers[i].mods);
        free(e->workers[i].inslots);
        free(e->workers[i].routes);
        free(e->workers[i].sorted);
        free(e->workers[i].fired);
        free(e->workers[i].mod_nodes);
    }
    free(e->workers);
    for (uint32_t bucket = 0; bucket < e->bucket_count; ++bucket) {
        Node *n = e->buckets[bucket];
        while (n != NULL) {
            Node *next = n->next;
            free(n->genes);
            free(n->values);
            free(n->marks);
            free(n->slots);
            free(n->estack);
            free(n->outputs);
            free(n->touched);
            free(n->inbuf);
            free(n->nextbuf);
            free(n);
            n = next;
        }
    }
    free(e->buckets);
    free(e->dirty);
    free(e->fired);
    free(e->mod_nodes);
    if (e->shards != NULL) {
        for (int i = 0; i < e->shard_count; ++i) {
            free(e->shards[i].next);
            mtx_destroy(&e->shards[i].lock);
        }
        free(e->shards);
    }
    cnd_destroy(&e->job_cv);
    cnd_destroy(&e->done_cv);
    mtx_destroy(&e->job_mtx);
    mtx_destroy(&e->mtx);
    free(e);
}

SM_EXPORT void smcgp_set_base_seed(void *handle, uint64_t seed)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    mtx_lock(&e->mtx);
    e->base_seed = seed ? seed : 0x2545F4914F6CDD1DULL;
    mtx_unlock(&e->mtx);
}

/* 设置“拒收路由输入”的 ID 区间 [base, base+size)：区间内节点只能被显式激活，
 * 别的神经元路由过来的消息一律丢弃。size=0 表示取消限制。 */
SM_EXPORT void smcgp_set_input_block(void *handle, int64_t base, int64_t size)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    mtx_lock(&e->mtx);
    if (size < 0) size = 0;
    if (base < 0) base = 0;
    e->block_base = base;
    e->block_size = size;
    mtx_unlock(&e->mtx);
}

/* 新建节点（随机种子图）；返回 1=新建，0=已存在或失败。 */
SM_EXPORT int smcgp_add_node(void *handle, int64_t id)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int created = 0;
    if (engine_find(e, id) == NULL) {
        created = node_get_or_create(e, id) != NULL ? 1 : 0;
    }
    mtx_unlock(&e->mtx);
    return created;
}

/* 设置图结构：layout 每 3 个 int 一个基因（op,c0,c1），params 每 3 个 double 一个基因。 */
SM_EXPORT int smcgp_set_genome(void *handle, int64_t id, const int32_t *layout,
                               const double *params, int count, uint64_t seed)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    if (count < 0) count = 0;
    mtx_lock(&e->mtx);
    Node *n = node_get_or_create(e, id);
    if (n == NULL) {
        mtx_unlock(&e->mtx);
        return 0;
    }
    if (seed != 0) n->rng = seed;
    if (!genome_ensure(n, count > 0 ? count : 1)) {
        mtx_unlock(&e->mtx);
        return 0;
    }
    atomic_fetch_sub(&e->gene_total, n->gene_count);
    n->gene_count = count;
    atomic_fetch_add(&e->gene_total, count);
    for (int i = 0; i < count; ++i) {
        Gene *gene = &n->genes[i];
        gene->op = layout ? layout[i * 3] : 0;
        gene->c0 = layout ? layout[i * 3 + 1] : 1;
        gene->c1 = layout ? layout[i * 3 + 2] : 1;
        gene->p0 = params ? params[i * 3] : 0.0;
        gene->p1 = params ? params[i * 3 + 1] : 0.0;
        gene->p2 = params ? params[i * 3 + 2] : 0.0;
    }
    mtx_unlock(&e->mtx);
    engine_mark_modified(e, n);
    return 1;
}

/* 导出图结构；返回该节点的基因总数（cap 不足时只复制 cap 个）。 */
SM_EXPORT int smcgp_get_genome(void *handle, int64_t id, int32_t *layout,
                               double *params, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return -1;
    mtx_lock(&e->mtx);
    Node *n = engine_find(e, id);
    if (n == NULL) {
        mtx_unlock(&e->mtx);
        return -1;
    }
    int limit = cap < n->gene_count ? cap : n->gene_count;
    for (int i = 0; i < limit; ++i) {
        if (layout) {
            layout[i * 3] = n->genes[i].op;
            layout[i * 3 + 1] = n->genes[i].c0;
            layout[i * 3 + 2] = n->genes[i].c1;
        }
        if (params) {
            params[i * 3] = n->genes[i].p0;
            params[i * 3 + 1] = n->genes[i].p1;
            params[i * 3 + 2] = n->genes[i].p2;
        }
    }
    int total = n->gene_count;
    mtx_unlock(&e->mtx);
    return total;
}

SM_EXPORT int smcgp_has_node(void *handle, int64_t id)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int found = engine_find(e, id) != NULL;
    mtx_unlock(&e->mtx);
    return found;
}

/* 投递一个输入值；节点不存在时按随机种子图创建。 */
SM_EXPORT int smcgp_activate(void *handle, int64_t id, double value)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    Node *n = node_get_or_create(e, id);
    int ok = 0;
    if (n != NULL && append_input(&n->inbuf, &n->in_count, &n->in_cap, value)) {
        ok = engine_push_dirty(e, n);
    }
    mtx_unlock(&e->mtx);
    return ok;
}

SM_EXPORT int smcgp_activate_many(void *handle, const int64_t *ids,
                                  const double *values, int count)
{
    Engine *e = (Engine *)handle;
    if (e == NULL || ids == NULL || values == NULL || count <= 0) return 0;
    /* 整批只抢一次全局锁：每拍要投递几千个输入，逐条加锁会把投递变成串行瓶颈。 */
    int ok = 0;
    mtx_lock(&e->mtx);
    for (int i = 0; i < count; ++i) {
        Node *n = node_get_or_create(e, ids[i]);
        if (n != NULL && append_input(&n->inbuf, &n->in_count, &n->in_cap, values[i])) {
            if (engine_push_dirty(e, n)) ok += 1;
        }
    }
    mtx_unlock(&e->mtx);
    return ok;
}

/* 整帧视觉位图：只把 bit=1 的像素（id = 下标）作为输入 1.0 投递。 */
SM_EXPORT int smcgp_activate_visual(void *handle, const unsigned char *bits, int count)
{
    Engine *e = (Engine *)handle;
    if (e == NULL || bits == NULL || count <= 0) return 0;
    int ok = 0;
    mtx_lock(&e->mtx);
    for (int i = 0; i < count; ++i) {
        if (!bits[i]) continue;
        Node *n = node_get_or_create(e, (int64_t)i);
        if (n != NULL && append_input(&n->inbuf, &n->in_count, &n->in_cap, 1.0)) {
            if (engine_push_dirty(e, n)) ok += 1;
        }
    }
    mtx_unlock(&e->mtx);
    return ok;
}

/* 是否落在“保留区间”里（视觉 / 操作感觉四组 / 刺激神经元等固定段）。
 * 保留区间内的节点不算“扩展神经元”，控制台只统计区间外的节点。 */
static int node_id_reserved(const Engine *e, int64_t id)
{
    for (int i = 0; i < e->reserved_count; ++i) {
        if (id >= e->reserved_start[i] && id < e->reserved_end[i]) return 1;
    }
    return 0;
}

/* 跑一轮：并行执行所有待处理输入的节点，并把输出汇总成下一轮输入。 */
SM_EXPORT int smcgp_run_round(void *handle)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    if (e->dirty_count <= 0) {
        e->fired_count = 0;
        atomic_store(&e->stats_run_total, 0);
        atomic_store(&e->stats_run_expanded, 0);
        atomic_store(&e->stats_pending_total, 0);
        atomic_store(&e->stats_pending_expanded, 0);
        mtx_unlock(&e->mtx);
        return 0;
    }
    Node **run_nodes = e->dirty;
    int run_count = e->dirty_count;
    e->dirty_count = 0;
    e->fired_count = 0;
    e->last_error = 0;
    mtx_unlock(&e->mtx);

    double t_start = e->debug_log ? mono_seconds() : 0.0;
    if (e->debug_log) {
        atomic_store(&e->round_emissions, 0);
        atomic_store(&e->dbg_node_ns, 0);
        atomic_store(&e->dbg_eval_ns, 0);
        atomic_store(&e->dbg_commit_ns, 0);
        atomic_store(&e->dbg_slowest_ns, 0);
        atomic_store(&e->dbg_slowest_id, 0);
        atomic_store(&e->dbg_slowest_genes, 0);
        atomic_store(&e->dbg_slowest_rebuild_ns, 0);
        atomic_store(&e->dbg_slowest_eval_ns, 0);
        atomic_store(&e->dbg_slowest_commit_ns, 0);
    }

    if (e->worker_count > 0) {
        /* 动态领取的粒度：核数越多切得越细，保证每条线程都有活干且尾部不空转。 */
        int batch = run_count / (e->worker_count * 16);
        if (batch < 1) batch = 1;
        if (batch > 64) batch = 64;
        mtx_lock(&e->job_mtx);
        e->run_nodes = run_nodes;
        e->run_count = run_count;
        e->batch = batch;
        atomic_store(&e->next_index, 0);
        e->job_seq += 1;
        e->workers_expected = e->worker_count;
        e->finished = 0;
        e->job_active = 1;
        cnd_broadcast(&e->job_cv);
        while (e->finished < e->workers_expected && !e->stopping) {
            cnd_wait(&e->done_cv, &e->job_mtx);
        }
        mtx_unlock(&e->job_mtx);
    } else {
        for (int i = 0; i < run_count; ++i) {
            run_nodes[i]->in_dirty = 0;
            node_run(e, &e->workers[0], run_nodes[i]);
        }
        worker_flush_registers(e, &e->workers[0]);
    }
    double t_parallel = e->debug_log ? mono_seconds() : 0.0;

    /* 控制台统计 1/2：本轮实际执行的神经元总数，以及其中扩展神经元的数量。
     * 直接遍历本轮 run_nodes（主线程，O(run_count) 次整数比较，开销可忽略）。 */
    long long run_expanded = 0;
    for (int i = 0; i < run_count; ++i) {
        if (!node_id_reserved(e, run_nodes[i]->id)) run_expanded += 1;
    }
    atomic_store(&e->stats_run_total, (long long)run_count);
    atomic_store(&e->stats_run_expanded, run_expanded);

    /* 本轮路由出来的输入提升为下一轮输入（此时所有线程已结束，分片无需再加锁）。 */
    mtx_lock(&e->mtx);
    for (int s = 0; s < e->shard_count; ++s) {
        Shard *shard = &e->shards[s];
        for (int i = 0; i < shard->next_count; ++i) {
            Node *n = shard->next[i];
            n->in_next = 0;
            double *buffer = n->inbuf;
            n->inbuf = n->nextbuf;
            n->nextbuf = buffer;
            int capacity = n->in_cap;
            n->in_cap = n->next_cap;
            n->next_cap = capacity;
            n->in_count = n->next_count;
            n->next_count = 0;
            engine_push_dirty(e, n);
        }
        shard->next_count = 0;
    }
    /* 控制台统计 2/2：下一轮待执行的神经元总数，以及其中扩展神经元的数量。 */
    long long pending_expanded = 0;
    for (int i = 0; i < e->dirty_count; ++i) {
        if (!node_id_reserved(e, e->dirty[i]->id)) pending_expanded += 1;
    }
    atomic_store(&e->stats_pending_total, (long long)e->dirty_count);
    atomic_store(&e->stats_pending_expanded, pending_expanded);
    mtx_unlock(&e->mtx);

    if (e->debug_log) {
        double t_done = mono_seconds();
        long long seq = atomic_fetch_add(&e->round_seq, 1) + 1;
        if (seq % 40 == 0) {
            /* 轮次诊断：并行段与合并段各自的耗时，用来判断到底是线程没活干还是在抢锁。 */
            fprintf(stderr,
                    "[smcgp_node] 轮次 %lld：节点 %d，发射 %lld，并行 %.2f ms，合并 %.2f ms，"
                    "总计 %.2f ms，线程 %d，节点耗时合计 %.1f ms（求值 %.1f / 自修改 %.1f），"
                    "最慢 节点 %lld（%lld 基因）%.2f ms（重建 %.2f / 求值 %.2f / 自修改 %.2f），"
                    "基因上限 %lld / 累计拦截 %lld 次，"
                    "全局基因 %lld / %lld，扩展 执行 %lld / 待执行 %lld\n",
                    seq, run_count, (long long)atomic_load(&e->round_emissions),
                    (t_parallel - t_start) * 1000.0, (t_done - t_parallel) * 1000.0,
                    (t_done - t_start) * 1000.0, e->worker_count,
                    (double)atomic_load(&e->dbg_node_ns) / 1e6,
                    (double)atomic_load(&e->dbg_eval_ns) / 1e6,
                    (double)atomic_load(&e->dbg_commit_ns) / 1e6,
                    (long long)atomic_load(&e->dbg_slowest_id),
                    (long long)atomic_load(&e->dbg_slowest_genes),
                    (double)atomic_load(&e->dbg_slowest_ns) / 1e6,
                    (double)atomic_load(&e->dbg_slowest_rebuild_ns) / 1e6,
                    (double)atomic_load(&e->dbg_slowest_eval_ns) / 1e6,
                    (double)atomic_load(&e->dbg_slowest_commit_ns) / 1e6,
                    (long long)e->max_genes,
                    (long long)atomic_load(&e->dbg_cap_hits),
                    (long long)atomic_load(&e->gene_total),
                    (long long)e->max_total_genes,
                    (long long)atomic_load(&e->stats_run_expanded),
                    (long long)atomic_load(&e->stats_pending_expanded));
            fflush(stderr);
        }
    }
    return run_count;
}

/* 取走本轮产生过输出的节点 ID（去重）；返回总数并清空。 */
SM_EXPORT int smcgp_take_fired(void *handle, int64_t *ids, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int total = e->fired_count;
    int limit = cap < total ? cap : total;
    if (ids) {
        for (int i = 0; i < limit; ++i) ids[i] = e->fired[i];
    }
    e->fired_count = 0;
    mtx_unlock(&e->mtx);
    return total;
}

/* 只读查看某个节点当前的待处理输入（不清空），供 Python 观测。 */
SM_EXPORT int smcgp_take_inputs(void *handle, int64_t id, double *values, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    Node *n = engine_find(e, id);
    int total = n ? n->in_count : 0;
    int limit = cap < total ? cap : total;
    if (values && n) {
        for (int i = 0; i < limit; ++i) values[i] = n->inbuf[i];
    }
    mtx_unlock(&e->mtx);
    return total;
}

/* 取走自上次调用以来图结构改过的节点 ID；返回总数并清空登记。 */
SM_EXPORT int smcgp_take_modified(void *handle, int64_t *ids, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int total = e->mod_node_count;
    int limit = cap < total ? cap : total;
    for (int i = 0; i < total; ++i) {
        Node *n = e->mod_nodes[i];
        if (i < limit && ids) ids[i] = n->id;
        n->modified = 0;
    }
    e->mod_node_count = 0;
    mtx_unlock(&e->mtx);
    return total;
}

/* 列出全部节点 ID；返回节点总数（cap 不足时只填 cap 个）。 */
SM_EXPORT int smcgp_list_nodes(void *handle, int64_t *ids, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int written = 0;
    for (uint32_t bucket = 0; bucket < e->bucket_count; ++bucket) {
        for (Node *n = e->buckets[bucket]; n != NULL; n = n->next) {
            if (ids && written < cap) ids[written] = n->id;
            written += 1;
        }
    }
    mtx_unlock(&e->mtx);
    return written;
}

/* 列出下一轮待执行节点 ID；返回总数（cap 不足时只填 cap 个）。 */
SM_EXPORT int smcgp_pending_nodes(void *handle, int64_t *ids, int cap)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int limit = cap < e->dirty_count ? cap : e->dirty_count;
    if (ids) {
        for (int i = 0; i < limit; ++i) ids[i] = e->dirty[i]->id;
    }
    int total = e->dirty_count;
    mtx_unlock(&e->mtx);
    return total;
}

/* 统计：节点总数与内部节点（基因）总数。 */
SM_EXPORT void smcgp_stats(void *handle, long long *nodes, long long *genes)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    if (nodes) *nodes = (long long)atomic_load(&e->node_count);
    if (genes) *genes = (long long)atomic_load(&e->gene_total);
}

/* 设置全局内部节点（基因）总数上限（默认 800 万）。 */
SM_EXPORT void smcgp_set_total_gene_limit(void *handle, long long limit)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    if (limit < 0) limit = 0;
    mtx_lock(&e->mtx);
    e->max_total_genes = (int64_t)limit;
    mtx_unlock(&e->mtx);
}

/* 下发保留区间（视觉 / 操作感觉四组 / 刺激神经元等固定段）。
 * 控制台统计只把区间之外的节点算作“扩展神经元”。 */
SM_EXPORT int smcgp_set_reserved_ranges(void *handle, const int64_t *starts,
                                        const int64_t *ends, int count)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    if (count < 0) count = 0;
    if (count > SM_MAX_RESERVED) count = SM_MAX_RESERVED;
    mtx_lock(&e->mtx);
    e->reserved_count = count;
    for (int i = 0; i < count; ++i) {
        int64_t start = starts ? starts[i] : 0;
        int64_t end = ends ? ends[i] : 0;
        if (end < start) end = start;
        e->reserved_start[i] = start;
        e->reserved_end[i] = end;
    }
    mtx_unlock(&e->mtx);
    return count;
}

/* 读取上一轮统计：执行总数 / 其中扩展神经元数；下一轮待执行总数 / 其中扩展神经元数。 */
SM_EXPORT void smcgp_activity(void *handle, long long *run_total, long long *run_expanded,
                              long long *pending_total, long long *pending_expanded)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    if (run_total) *run_total = (long long)atomic_load(&e->stats_run_total);
    if (run_expanded) *run_expanded = (long long)atomic_load(&e->stats_run_expanded);
    if (pending_total) *pending_total = (long long)atomic_load(&e->stats_pending_total);
    if (pending_expanded) *pending_expanded = (long long)atomic_load(&e->stats_pending_expanded);
}
