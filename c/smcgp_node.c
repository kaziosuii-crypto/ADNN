/*
 * SMCGP14 神经元 + 群体调度的 C 实现（ctypes 可直接调用）。
 *
 * 与 compute/node.py 完全一致的边界：
 *   - 14 种指令：INP/OUTPUT/DADD/DMULT/CONST/IFLTE/INDX + ADD/DEL/MOV/CHC/CHF/FLR/CHP；
 *   - 相对连接 c >= 1 只引用前方节点，越界/前向/自引用一律读作 0；
 *   - 一轮内“读取旧图、轮末按登记顺序提交修改”，只有 OUTPUT 追溯到的节点才执行；
 *   - 自修改区间左闭右开、越界夹紧，ADD 单轮最多 32 个节点，
 *     CHC 用 p1 % 2 选连接，CHF 用 p1 % 14，CHP 用 p1 % 3。
 *
 * 群体层只做最小调度：每个节点一份待处理输入表，一轮结束后按路由规则
 * （目标编码向零截断取整、负值取绝对值、对 10^dps 取模）把 emission 汇总到
 * 目标节点的下一轮输入。图结构落盘、JSON、回调派发全部留在 Python。
 */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <threads.h>

#if defined(_WIN32)
#define SM_EXPORT __declspec(dllexport)
#else
#define SM_EXPORT __attribute__((visibility("default")))
#endif

#define SM_OP_COUNT 14
#define SM_MAX_ADD_GENES 32
#define SM_RANDOM_RANGE 16
#define SM_DEFAULT_BUCKETS (1u << 16)

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
    /* 本轮求值暂存（长度按 gene_cap 扩容） */
    double *values;
    uint8_t *marks;
    int *slots;
    int *estack;
    int estack_cap;
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

typedef struct Engine {
    Node **buckets;
    uint32_t bucket_count;
    mtx_t mtx;
    int dps;
    int64_t scale;
    uint64_t base_seed;
    Node **dirty;
    int dirty_count;
    int dirty_cap;
    Node **next_dirty;
    int next_dirty_count;
    int next_dirty_cap;
    int64_t *fired;
    int fired_count;
    int fired_cap;
    Node **mod_nodes;   /* 自上次 take_modified 以来图结构改过的节点 */
    int mod_node_count;
    int mod_node_cap;
    Emission *emits;
    int emit_count;
    int emit_cap;
    Mod *mods;
    int mod_count;
    int mod_cap;
    double *inslots;
    int inslots_cap;
    long long node_count;
    long long gene_total;
    int last_error; /* 0=正常，1=内存不足 */
} Engine;

/* ---------- 通用工具 ---------- */

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

/* 一次性扩容某节点的全部求值暂存（值缓存、状态标记、INP 槽位、显式求值栈）。 */
static int scratch_ensure(Node *n, int need)
{
    if (need <= 0) need = 1;
    if (n->values == NULL || n->estack_cap < need * 8 + 32) {
        double *values = realloc(n->values, (size_t)need * sizeof(double));
        if (!values) return 0;
        n->values = values;
        uint8_t *marks = realloc(n->marks, (size_t)need * sizeof(uint8_t));
        if (!marks) return 0;
        n->marks = marks;
        int *slots = realloc(n->slots, (size_t)need * sizeof(int));
        if (!slots) return 0;
        n->slots = slots;
        int stack_cap = need * 8 + 32;
        int *estack = realloc(n->estack, (size_t)stack_cap * sizeof(int));
        if (!estack) return 0;
        n->estack = estack;
        n->estack_cap = stack_cap;
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

/* 取节点，不存在时按“随机种子图”创建；调用方必须已持有引擎锁。
 * 随机种子图 = INP + 1~4 个随机节点 + 一个随机回连的 OUTPUT，
 * 保证新神经元既能收输入也能产生输出，避免整张网络都发往同一个 ID。 */
static Gene random_gene(Node *n);
static void engine_mark_modified(Engine *e, Node *n);

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
    e->node_count += 1;
    e->gene_total += count;
    engine_mark_modified(e, n);
    return n;
}

/* ---------- 输入缓冲 ---------- */

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

static int engine_push_next(Engine *e, Node *n)
{
    if (n->in_next) return 1;
    if (!push_node(&e->next_dirty, &e->next_dirty_count, &e->next_dirty_cap, n)) return 0;
    n->in_next = 1;
    return 1;
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
    if (n->modified) return;
    n->modified = 1;
    if (!push_node(&e->mod_nodes, &e->mod_node_count, &e->mod_node_cap, n)) {
        e->last_error = 1;
    }
}

static int engine_push_emit(Engine *e, double value, double destination)
{
    if (!grow_bytes((void **)&e->emits, &e->emit_cap, e->emit_count + 1, sizeof(Emission))) return 0;
    e->emits[e->emit_count].value = value;
    e->emits[e->emit_count].destination = destination;
    e->emit_count += 1;
    return 1;
}

static int engine_push_mod(Engine *e, int index, int op, double p0, double p1, double p2)
{
    if (!grow_bytes((void **)&e->mods, &e->mod_cap, e->mod_count + 1, sizeof(Mod))) return 0;
    Mod *item = &e->mods[e->mod_count++];
    item->index = index;
    item->op = op;
    item->p0 = p0;
    item->p1 = p1;
    item->p2 = p2;
    return 1;
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

static int genome_insert_random(Engine *e, Node *n, int position, int count)
{
    if (count <= 0) return 0;
    if (!genome_ensure(n, n->gene_count + count)) return 0;
    memmove(n->genes + position + count, n->genes + position,
            (size_t)(n->gene_count - position) * sizeof(Gene));
    for (int i = 0; i < count; ++i) {
        n->genes[position + i] = random_gene(n);
    }
    n->gene_count += count;
    e->gene_total += count;
    engine_mark_modified(e, n);
    return 1;
}

static int genome_delete(Engine *e, Node *n, int start, int end)
{
    if (end <= start) return 0;
    memmove(n->genes + start, n->genes + end, (size_t)(n->gene_count - end) * sizeof(Gene));
    int removed = end - start;
    n->gene_count -= removed;
    e->gene_total -= removed;
    engine_mark_modified(e, n);
    return 1;
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
        for (int i = 0; i < count; ++i) out[i] = 0.0;
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
    for (int slot = 0; slot < count; ++slot) {
        out[slot] = values[slot % total];
    }
}

static double compute_value(Engine *e, Node *n, int index)
{
    Gene *gene = &n->genes[index];
    int op = gene->op;
    if (op == 0) { /* INP */
        int slot = n->slots[index];
        if (slot < 0 || slot >= n->inp_total) return 0.0;
        return e->inslots[slot];
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
            engine_push_mod(e, index, op, gene->p0, gene->p1, gene->p2);
            return a;
        }
        return b;
    }
    return 0.0;
}

static double eval_index(Engine *e, Node *n, int index)
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
        n->values[current] = compute_value(e, n, current);
    }
    return n->marks[index] == 2 ? n->values[index] : 0.0;
}

/* ---------- 自修改提交 ---------- */

static int apply_mod(Engine *e, Node *n, const Mod *mod)
{
    int size = n->gene_count;
    int index = mod->index;
    int op = mod->op;
    if (op == 7) { /* ADD */
        int start = clamp_int(index + (int)mod->p0, 0, size);
        int count = clamp_int((int)mod->p1, 0, SM_MAX_ADD_GENES);
        return genome_insert_random(e, n, start, count);
    }
    if (op == 8) { /* DEL */
        int start = clamp_int(index + (int)mod->p0, 0, size);
        int end = clamp_int(start + (int)mod->p1, start, size);
        return genome_delete(e, n, start, end);
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
        engine_mark_modified(e, n);
        return 1;
    }
    if (op == 10) { /* CHC */
        int target = clamp_int(index + (int)mod->p0, 0, size - 1);
        if (size <= 0) return 0;
        int link = (int)mod->p2;
        if (link < 1) link = 1;
        if (((int)mod->p1) % 2 == 0) n->genes[target].c0 = link;
        else n->genes[target].c1 = link;
        engine_mark_modified(e, n);
        return 1;
    }
    if (op == 11) { /* CHF */
        int target = clamp_int(index + (int)mod->p0, 0, size - 1);
        if (size <= 0) return 0;
        int value = ((int)mod->p1) % SM_OP_COUNT;
        if (value < 0) value += SM_OP_COUNT;
        n->genes[target].op = value;
        engine_mark_modified(e, n);
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
        engine_mark_modified(e, n);
        return 1;
    }
    return 0;
}

static void commit_mods(Engine *e, Node *n)
{
    int position = 0;
    while (position < e->mod_count) {
        Mod mod = e->mods[position++];
        if (mod.op == 12) { /* FLR：作废其后尚未提交的请求，本身不改图 */
            e->mod_count = 0;
            return;
        }
        apply_mod(e, n, &mod);
    }
    e->mod_count = 0;
}

/* ---------- 单个神经元的一轮执行 ---------- */

static void node_run(Engine *e, Node *n)
{
    if (n->gene_count <= 0) return;
    if (!scratch_ensure(n, n->gene_count)) {
        e->last_error = 1;
        return;
    }
    /* 1. 建立 INP 槽位映射 */
    int slot = 0;
    memset(n->slots, 0xFF, (size_t)n->gene_count * sizeof(int));
    for (int i = 0; i < n->gene_count; ++i) {
        if (n->genes[i].op == 0) n->slots[i] = slot++;
    }
    n->inp_total = slot;
    /* 2. 输入分配 */
    if (slot > 0) {
        if (!grow_bytes((void **)&e->inslots, &e->inslots_cap, slot, sizeof(double))) {
            e->last_error = 1;
            return;
        }
        distribute_inputs(n->inbuf, n->in_count, slot, e->inslots);
    }
    /* 3. 清理本轮暂存 */
    memset(n->marks, 0, (size_t)n->gene_count * sizeof(uint8_t));
    e->emit_count = 0;
    e->mod_count = 0;
    /* 4. 只有 OUTPUT 驱动计算 */
    int produced = 0;
    for (int i = 0; i < n->gene_count; ++i) {
        if (n->genes[i].op != 1) continue;
        double value = n->genes[i].c0 >= 1
                       ? eval_index(e, n, i - n->genes[i].c0) : 0.0;
        double destination = n->genes[i].c1 >= 1
                       ? eval_index(e, n, i - n->genes[i].c1) : 0.0;
        if (engine_push_emit(e, value, destination)) produced += 1;
    }
    /* 5. 轮末提交自修改 */
    commit_mods(e, n);
    /* 6. 路由本节点产生的消息到目标节点的下一轮输入 */
    if (produced > 0) {
        engine_push_fired(e, n->id);
        for (int i = 0; i < e->emit_count; ++i) {
            double destination = e->emits[i].destination;
            if (!isfinite(destination)) destination = 0.0;
            if (destination > 1e18) destination = 1e18;
            if (destination < -1e18) destination = -1e18;
            int64_t target = (int64_t)destination; /* 向零截断 */
            if (target < 0) target = -target;
            target %= e->scale;
            Node *to = node_get_or_create(e, target);
            if (to == NULL) continue;
            if (!append_input(&to->nextbuf, &to->next_count, &to->next_cap, e->emits[i].value)) {
                e->last_error = 1;
                continue;
            }
            engine_push_next(e, to);
        }
    }
    /* 7. 本轮输入已消费 */
    n->in_count = 0;
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
    if (mtx_init(&e->mtx, mtx_plain) != thrd_success) {
        free(e->buckets);
        free(e);
        return NULL;
    }
    if (dps < 1) dps = 1;
    if (dps > 9) dps = 9;
    e->dps = dps;
    e->scale = 1;
    for (int i = 0; i < dps; ++i) e->scale *= 10;
    e->base_seed = 0x2545F4914F6CDD1DULL;
    return e;
}

SM_EXPORT void smcgp_destroy(void *handle)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return;
    for (uint32_t bucket = 0; bucket < e->bucket_count; ++bucket) {
        Node *n = e->buckets[bucket];
        while (n != NULL) {
            Node *next = n->next;
            free(n->genes);
            free(n->values);
            free(n->marks);
            free(n->slots);
            free(n->estack);
            free(n->inbuf);
            free(n->nextbuf);
            free(n);
            n = next;
        }
    }
    free(e->buckets);
    free(e->dirty);
    free(e->next_dirty);
    free(e->fired);
    free(e->mod_nodes);
    free(e->emits);
    free(e->mods);
    free(e->inslots);
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

/* 新建节点（默认种子图 INP -> OUTPUT）；返回 1=新建，0=已存在或失败。 */
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
    e->gene_total -= n->gene_count;
    n->gene_count = count;
    e->gene_total += count;
    for (int i = 0; i < count; ++i) {
        Gene *gene = &n->genes[i];
        gene->op = layout ? layout[i * 3] : 0;
        gene->c0 = layout ? layout[i * 3 + 1] : 1;
        gene->c1 = layout ? layout[i * 3 + 2] : 1;
        gene->p0 = params ? params[i * 3] : 0.0;
        gene->p1 = params ? params[i * 3 + 1] : 0.0;
        gene->p2 = params ? params[i * 3 + 2] : 0.0;
    }
    engine_mark_modified(e, n);
    mtx_unlock(&e->mtx);
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

/* 投递一个输入值；节点不存在时按默认种子图创建。 */
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
    if (count <= 0) return 0;
    int ok = 0;
    for (int i = 0; i < count; ++i) {
        if (smcgp_activate(handle, ids[i], values[i])) ok += 1;
    }
    return ok;
}

/* 整帧视觉位图：只把 bit=1 的像素（id = 下标）作为输入 1.0 投递。 */
SM_EXPORT int smcgp_activate_visual(void *handle, const unsigned char *bits, int count)
{
    if (bits == NULL || count <= 0) return 0;
    int ok = 0;
    for (int i = 0; i < count; ++i) {
        if (bits[i]) {
            if (smcgp_activate(handle, (int64_t)i, 1.0)) ok += 1;
        }
    }
    return ok;
}

/* 跑一轮：执行所有有待处理输入的节点，并把输出汇总成下一轮输入。 */
SM_EXPORT int smcgp_run_round(void *handle)
{
    Engine *e = (Engine *)handle;
    if (e == NULL) return 0;
    mtx_lock(&e->mtx);
    int executed = 0;
    e->fired_count = 0;
    e->last_error = 0;
    if (e->dirty_count > 0) {
        Node **run = e->dirty;
        int run_count = e->dirty_count;
        e->dirty_count = 0;
        for (int i = 0; i < run_count; ++i) {
            Node *n = run[i];
            n->in_dirty = 0;
            node_run(e, n);
            executed += 1;
        }
        /* 把本轮路由出来的输入提升为下一轮输入 */
        for (int i = 0; i < e->next_dirty_count; ++i) {
            Node *n = e->next_dirty[i];
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
        e->next_dirty_count = 0;
    }
    mtx_unlock(&e->mtx);
    return executed;
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

/* 取走自上次调用以来图结构改过的节点 ID；返回总数并清空登记。
 * cap 需要足够大（Python 侧按 65536 传），否则超出部分只清标记不返回。 */
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
    mtx_lock(&e->mtx);
    if (nodes) *nodes = e->node_count;
    if (genes) *genes = e->gene_total;
    mtx_unlock(&e->mtx);
}
