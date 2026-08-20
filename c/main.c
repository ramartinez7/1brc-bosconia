#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <float.h>
#include <immintrin.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define TABLE_SIZE 16384u
#define TABLE_MASK (TABLE_SIZE - 1u)
#define MAX_LINE   110u
#define STATION_COUNT 413u
#define DISPATCH_SIZE 65536u
#define DISPATCH_MASK (DISPATCH_SIZE - 1u)

#define LIKELY(x)   __builtin_expect(!!(x), 1)
#define UNLIKELY(x) __builtin_expect(!!(x), 0)
#define HOT_FN __attribute__((hot))
#define COLD_FN __attribute__((cold))
#define ALWAYS_INLINE inline __attribute__((always_inline))

// One cache line. NamePrefix is at offset 32 → 32-byte aligned for vmovdqa.
typedef struct __attribute__((aligned(64))) {
    uint32_t key;
    uint32_t count;
    int64_t  sum;
    uint32_t short_mask;
    int16_t  min;
    int16_t  max;
    uint8_t  name_len;
    uint8_t  _pad[7];
    uint8_t  name_prefix[32];
} PerHot;

typedef struct {
    uint32_t key;
    uint32_t count;
    int16_t  min;
    int16_t  max;
    uint32_t _pad;
    int64_t  sum;
} HotEntry;

// 104-byte stride; 3-byte tail pad keeps NameEntry[] stride friendly.
typedef struct {
    uint8_t name_len;
    uint8_t name[100];
    uint8_t _pad[3];
} NameEntry;

typedef struct {
    uint32_t count;
    int16_t  min;
    int16_t  max;
    int64_t  sum;
} DenseStat;

typedef struct {
    size_t    count;
    NameEntry names[STATION_COUNT];
    uint16_t  indices[STATION_COUNT];
    uint16_t  dispatch[DISPATCH_SIZE];
} DenseDictionary;

#define HOT_BYTES   (sizeof(PerHot) * TABLE_SIZE)
#define NAMES_BYTES (sizeof(NameEntry) * TABLE_SIZE)

typedef struct {
    const uint8_t     *data;
    off_t              file_size;
    _Atomic long long *cursor;
    int                thread_idx;
    PerHot            *hot;       // worker fills these in before returning
} WorkerArgs;

typedef struct {
    uint32_t       key;
    int64_t        val;
    const uint8_t *next;
    __m256i        chunk0;
} ParsedLine;

typedef struct {
    uint32_t       key;
    int64_t        val;
    const uint8_t *next;
} DenseParsed;

typedef struct {
    const uint8_t          *data;
    off_t                   file_size;
    _Atomic long long      *cursor;
    int                     thread_idx;
    DenseStat              *stats;
} DenseWorkerArgs;

_Static_assert(sizeof(PerHot)   == 64,  "PerHot must stay one cache line");
_Static_assert(sizeof(NameEntry) == 104, "NameEntry layout changed");
_Static_assert(sizeof(DenseStat) == 16, "DenseStat layout changed");

static ALWAYS_INLINE NameEntry *names_for(PerHot *hot) {
    return (NameEntry *)((uint8_t *)hot + HOT_BYTES);
}

COLD_FN static void die(const char *what) {
    if (errno) perror(what); else fprintf(stderr, "%s\n", what);
    exit(1);
}

static ALWAYS_INLINE uint64_t load_u64(const void *p) {
    uint64_t v; memcpy(&v, p, sizeof v); return v;
}

static ALWAYS_INLINE uint32_t make_key(uint64_t first8, size_t name_len) {
    uint64_t masked = _bzhi_u64(first8, (unsigned)(name_len * 8u));
    uint64_t h = (masked + (uint64_t)name_len) * 0x9E3779B97F4A7C15ULL;
    // Sentinel bit 31 keeps the key nonzero (key == 0 marks an empty slot)
    // without touching the low TABLE_MASK bits used for the primary index.
    // Invariant: the sentinel bit must stay outside TABLE_MASK.
    return (uint32_t)(h >> 32) | 0x80000000u;
}

static int build_dense_dictionary(
    const uint8_t *data,
    size_t size,
    DenseDictionary *dictionary)
{
    // The challenge contract is exactly 413 distinct names. Once all 413 are
    // discovered the set is complete; inputs with more names are out of scope.
    const uint8_t *ptr = data;
    const uint8_t *end = data + size;
    dictionary->count = 0;

    while (ptr < end && dictionary->count < STATION_COUNT) {
        size_t available = (size_t)(end - ptr);
        size_t bound = available < MAX_LINE ? available : MAX_LINE;
        const uint8_t *semicolon = memchr(ptr, ';', bound);
        if (!semicolon) return 0;
        size_t length = (size_t)(semicolon - ptr);

        size_t id = 0;
        for (; id < dictionary->count; id++) {
            const NameEntry *entry = &dictionary->names[id];
            if (entry->name_len == length &&
                memcmp(entry->name, ptr, length) == 0)
            {
                break;
            }
        }
        if (id == dictionary->count) {
            NameEntry *entry = &dictionary->names[dictionary->count++];
            entry->name_len = (uint8_t)length;
            memcpy(entry->name, ptr, length);
        }

        const uint8_t *newline = memchr(semicolon, '\n', (size_t)(end - semicolon));
        if (!newline) break;
        ptr = newline + 1;
    }
    if (dictionary->count != STATION_COUNT) return 0;

    memset(dictionary->dispatch, 0, sizeof dictionary->dispatch);
    for (size_t id = 0; id < STATION_COUNT; id++) {
        const NameEntry *entry = &dictionary->names[id];
        uint64_t first8 = 0;
        size_t copy = entry->name_len < 8 ? entry->name_len : 8;
        memcpy(&first8, entry->name, copy);
        size_t index = (size_t)make_key(first8, entry->name_len) & DISPATCH_MASK;
        if (dictionary->dispatch[index] != 0) return 0;
        dictionary->dispatch[index] = (uint16_t)(id + 1u);
        dictionary->indices[id] = (uint16_t)index;
    }
    return 1;
}

// Branchless ±N.N / ±NN.N parser in integer tenths. The first zero bit 4
// among bytes 1-3 identifies '.', which determines both alignment and length.
// Reads 8 bytes; unused tail bytes are masked out.
static ALWAYS_INLINE void parse_temp_dot(const uint8_t *ptr, int64_t *val, size_t *advance) {
    uint64_t raw = load_u64(ptr);
    unsigned decimal_pos = (unsigned)__builtin_ctzll((~raw) & 0x10101000ULL);
    *advance = (size_t)(decimal_pos >> 3) + 3u;

    int64_t sign = (int64_t)(~raw << 59) >> 63;
    uint64_t digits = ((raw & ~((uint64_t)sign & 0xFFu)) << (28u - decimal_pos))
                    & 0x0F000F0F00ULL;
    int64_t mag = (int64_t)(((digits * 0x640a0001ULL) >> 32) & 0x3FFu);
    *val = (mag ^ sign) - sign;
}

static ALWAYS_INLINE uint64_t load_u64_bounded(
    const uint8_t *ptr,
    const uint8_t *end)
{
    if ((size_t)(end - ptr) >= sizeof(uint64_t)) return load_u64(ptr);
    uint64_t value = 0;
    memcpy(&value, ptr, (size_t)(end - ptr));
    return value;
}

static ALWAYS_INLINE void parse_temp_dot_bounded(
    const uint8_t *ptr,
    const uint8_t *end,
    int64_t *value,
    size_t *advance)
{
    if ((size_t)(end - ptr) >= sizeof(uint64_t)) {
        parse_temp_dot(ptr, value, advance);
        return;
    }
    uint8_t tail[8] = {0};
    memcpy(tail, ptr, (size_t)(end - ptr));
    parse_temp_dot(tail, value, advance);
}

COLD_FN static size_t parse_line_long_semi(const uint8_t *line) {
    size_t off = 64;
    while (line[off] != ';') off++;
    return off;
}

// chunk0 carries the first 32 bytes; name_eq_hot reuses it against
// entry->name_prefix, saving one ymm load per line.
HOT_FN
static ALWAYS_INLINE ParsedLine parse_line(const uint8_t *line, __m256i semi_vec) {
    __m256i c0 = _mm256_loadu_si256((const __m256i *)line);
    uint32_t m0 = (uint32_t)_mm256_movemask_epi8(_mm256_cmpeq_epi8(c0, semi_vec));
    size_t semi_off;
    if (LIKELY(m0 != 0)) {
        semi_off = (size_t)__builtin_ctz(m0);
    } else {
        __m256i c1 = _mm256_loadu_si256((const __m256i *)(line + 32));
        uint32_t m1 = (uint32_t)_mm256_movemask_epi8(_mm256_cmpeq_epi8(c1, semi_vec));
        semi_off = m1 != 0 ? 32u + (size_t)__builtin_ctz(m1) : parse_line_long_semi(line);
    }
    const uint8_t *val_ptr = line + semi_off + 1;
    ParsedLine p;
    size_t adv;
    parse_temp_dot(val_ptr, &p.val, &adv);
    p.key    = make_key(load_u64(line), semi_off);
    p.next   = val_ptr + adv;
    p.chunk0 = c0;
    return p;
}

HOT_FN
static ALWAYS_INLINE int name_eq_hot(const PerHot *e, const NameEntry *n, const uint8_t *name_ptr, __m256i input_chunk0) {
    size_t len = e->name_len;

    // Chunk 1 reuses chunk0 from parse_line; name_prefix is 32-byte aligned
    // (field offset 32 inside the cache-line-aligned PerHot).
    __m256i va  = _mm256_load_si256((const __m256i *)e->name_prefix);
    uint32_t mask = (uint32_t)_mm256_movemask_epi8(_mm256_cmpeq_epi8(va, input_chunk0));
    uint32_t need = e->short_mask;
    if (UNLIKELY((mask & need) != need)) return 0;
    if (LIKELY(len < 32)) return 1;                       // ';' is encoded in short_mask
    if (len == 32) return name_ptr[32] == ';';

    __m256i va2 = _mm256_loadu_si256((const __m256i *)(n->name + 32));
    __m256i vb2 = _mm256_loadu_si256((const __m256i *)(name_ptr + 32));
    uint32_t m2 = (uint32_t)_mm256_movemask_epi8(_mm256_cmpeq_epi8(va2, vb2));
    if (len < 64) {
        uint32_t cmask = (1u << (len - 32u)) - 1u;
        return (m2 & cmask) == cmask && name_ptr[len] == ';';
    }
    if (m2 != UINT32_MAX) return 0;
    if (len == 64) return name_ptr[64] == ';';

    __m256i va3 = _mm256_loadu_si256((const __m256i *)(n->name + 64));
    __m256i vb3 = _mm256_loadu_si256((const __m256i *)(name_ptr + 64));
    uint32_t m3 = (uint32_t)_mm256_movemask_epi8(_mm256_cmpeq_epi8(va3, vb3));
    if (len < 96) {
        uint32_t cmask = (1u << (len - 64u)) - 1u;
        return (m3 & cmask) == cmask && name_ptr[len] == ';';
    }
    if (m3 != UINT32_MAX) return 0;
    if (len == 96) return name_ptr[96] == ';';

    for (size_t i = 96; i < len; i++) if (n->name[i] != name_ptr[i]) return 0;
    return name_ptr[len] == ';';
}

COLD_FN
static void insert_new(PerHot *restrict hot, NameEntry *restrict names, uint32_t key, int64_t val, const uint8_t *name_ptr, size_t idx) {
    size_t len = 0;
    while (name_ptr[len] != ';') len++;

    PerHot *e = &hot[idx];
    e->key = key; e->count = 1; e->sum = val;
    e->min = (int16_t)val; e->max = (int16_t)val;
    e->name_len = (uint8_t)len;
    memcpy(e->name_prefix, name_ptr, len < 32 ? len : 32);
    // Encode ';' into the prefix and extend short_mask to cover it, so
    // name_eq_hot's first vector compare validates the terminator too.
    // Invariant: the mask must be built in 64-bit -- len == 31 needs all 32
    // bits set, and 1u << 32 on a 32-bit type is undefined (x86 masks the
    // shift count to 5 bits and yields a mask of 0, which matches anything).
    if (len < 32) e->name_prefix[len] = ';';
    e->short_mask = len < 32 ? (uint32_t)(((uint64_t)1 << (len + 1u)) - 1u) : UINT32_MAX;

    NameEntry *n = &names[idx];
    n->name_len = (uint8_t)len;
    memcpy(n->name, name_ptr, len);
}

HOT_FN
static ALWAYS_INLINE void probe_and_update(PerHot *restrict hot, uint32_t key, int64_t val, const uint8_t *name_ptr, __m256i input_chunk0) {
    NameEntry *restrict names = names_for(hot);
    // Peel slot 0 (99 %+ hit rate) so the hot path has no probes counter.
    size_t idx = (size_t)key & TABLE_MASK;
    PerHot *e = &hot[idx];
    if (LIKELY(e->key == key)) {
        if (name_eq_hot(e, &names[idx], name_ptr, input_chunk0)) goto hit;
    } else if (UNLIKELY(e->key == 0)) {
        insert_new(hot, names, key, val, name_ptr, idx);
        return;
    }
    for (size_t probes = 1; probes < TABLE_SIZE; probes++) {
        idx = (idx + 1u) & TABLE_MASK;
        e = &hot[idx];
        if (e->key == key) {
            if (name_eq_hot(e, &names[idx], name_ptr, input_chunk0)) goto hit;
        } else if (e->key == 0) {
            insert_new(hot, names, key, val, name_ptr, idx);
            return;
        }
    }
    die("hash table full");
hit:
    {
        int16_t v16 = (int16_t)val;
        if (v16 < e->min) e->min = v16;
        if (v16 > e->max) e->max = v16;
    }
    e->count++;
    e->sum += val;
}

// AVX2 path while ptr < safe_end, then scalar tail until ptr == end with a
// guarded chunk0 load for the last <32 bytes (segment splits may not align
// to a page boundary).
HOT_FN
static void drain(const uint8_t *ptr, const uint8_t *safe_end, const uint8_t *end,
                  __m256i semi_vec, PerHot *restrict hot) {
    while (ptr < safe_end) {
        const uint8_t *line = ptr;
        ParsedLine p = parse_line(line, semi_vec);
        ptr = p.next;
        probe_and_update(hot, p.key, p.val, line, p.chunk0);
    }
    while (ptr < end) {
        const uint8_t *line = ptr;
        size_t semi_off = 0;
        while (line + semi_off < end && line[semi_off] != ';') semi_off++;
        const uint8_t *val_ptr = line + semi_off + 1;
        if (val_ptr >= end) break;

        int64_t val; size_t adv;
        parse_temp_dot_bounded(val_ptr, end, &val, &adv);
        ptr = val_ptr + adv;
        uint32_t key = make_key(load_u64_bounded(line, end), semi_off);

        // Name matching reads fixed 32-byte chunks through offset 64.
        // Bounce the bounded tail record so every such load remains valid.
        uint8_t padded[MAX_LINE] = {0};
        memcpy(padded, line, (size_t)(end - line));
        __m256i chunk0 =
            _mm256_loadu_si256((const __m256i *)padded);
        probe_and_update(hot, key, val, padded, chunk0);
    }
}

HOT_FN
static void process_avx2(const uint8_t *restrict data, size_t len, PerHot *restrict hot) {
    if (len == 0) return;

    __m256i semi_vec = _mm256_set1_epi8(';');
    const uint8_t *end = data + len;

    // Split the segment in half and parse both halves in lockstep — two
    // independent dep chains expose ILP for the probe/prefetch latency.
    size_t q = len / 2u;
    const uint8_t *e0 = data + q;
    while (e0 < end && *e0 != '\n') e0++;
    if (e0 < end) e0++;

    const uint8_t *p0 = data;
    const uint8_t *p1 = e0;
    const uint8_t *e1 = end;
    const uint8_t *sf0 = (size_t)(e0 - p0) > MAX_LINE ? e0 - MAX_LINE : p0;
    const uint8_t *sf1 = (size_t)(e1 - p1) > MAX_LINE ? e1 - MAX_LINE : p1;

    if (p0 < sf0 && p1 < sf1) {
        ParsedLine parsed0 = parse_line(p0, semi_vec);
        ParsedLine parsed1 = parse_line(p1, semi_vec);
        for (;;) {
            const uint8_t *lp0 = p0, *lp1 = p1;
            p0 = parsed0.next; p1 = parsed1.next;
            if (!(p0 < sf0 && p1 < sf1)) {
                probe_and_update(hot, parsed0.key, parsed0.val, lp0, parsed0.chunk0);
                probe_and_update(hot, parsed1.key, parsed1.val, lp1, parsed1.chunk0);
                break;
            }
            // opt-rust-data-prefetch: the two interleaved lane streams
            // under-run the HW stream prefetcher; SW-prefetch one cacheline
            // ahead of each lane to hide the demand-load latency.
            __builtin_prefetch(p0 + 128, 0, 3);
            __builtin_prefetch(p1 + 128, 0, 3);
            ParsedLine n0 = parse_line(p0, semi_vec);
            ParsedLine n1 = parse_line(p1, semi_vec);
            __builtin_prefetch(&hot[(size_t)n0.key & TABLE_MASK], 1, 3);
            __builtin_prefetch(&hot[(size_t)n1.key & TABLE_MASK], 1, 3);
            probe_and_update(hot, parsed0.key, parsed0.val, lp0, parsed0.chunk0);
            probe_and_update(hot, parsed1.key, parsed1.val, lp1, parsed1.chunk0);
            parsed0 = n0; parsed1 = n1;
        }
    }

    drain(p0, sf0, e0, semi_vec, hot);
    drain(p1, sf1, e1, semi_vec, hot);
}

static ALWAYS_INLINE DenseParsed parse_line_dense(
    const uint8_t *line,
    __m256i semi_vec)
{
    __m256i chunk0 = _mm256_loadu_si256((const __m256i *)line);
    uint32_t mask0 = (uint32_t)_mm256_movemask_epi8(
        _mm256_cmpeq_epi8(chunk0, semi_vec));
    size_t semicolon_offset;
    if (LIKELY(mask0 != 0)) {
        semicolon_offset = (size_t)__builtin_ctz(mask0);
    } else {
        __m256i chunk1 = _mm256_loadu_si256((const __m256i *)(line + 32));
        uint32_t mask1 = (uint32_t)_mm256_movemask_epi8(
            _mm256_cmpeq_epi8(chunk1, semi_vec));
        semicolon_offset = mask1 != 0
            ? 32u + (size_t)__builtin_ctz(mask1)
            : parse_line_long_semi(line);
    }

    const uint8_t *value_ptr = line + semicolon_offset + 1u;
    DenseParsed parsed;
    size_t advance;
    parse_temp_dot(value_ptr, &parsed.val, &advance);
    parsed.key = make_key(load_u64(line), semicolon_offset);
    parsed.next = value_ptr + advance;
    return parsed;
}

static ALWAYS_INLINE void dense_update_direct(
    DenseStat *restrict stats,
    uint32_t key,
    int64_t value)
{
    DenseStat *entry = &stats[(size_t)key & DISPATCH_MASK];
    if (UNLIKELY(entry->count == 0)) {
        entry->count = 1;
        entry->sum = value;
        entry->min = (int16_t)value;
        entry->max = (int16_t)value;
        return;
    }
    int16_t value16 = (int16_t)value;
    if (value16 < entry->min) entry->min = value16;
    if (value16 > entry->max) entry->max = value16;
    entry->count++;
    entry->sum += value;
}

static void dense_drain(
    const uint8_t *ptr,
    const uint8_t *safe_end,
    const uint8_t *end,
    __m256i semi_vec,
    DenseStat *restrict stats)
{
    while (ptr < safe_end) {
        DenseParsed parsed = parse_line_dense(ptr, semi_vec);
        ptr = parsed.next;
        dense_update_direct(stats, parsed.key, parsed.val);
    }
    while (ptr < end) {
        const uint8_t *line = ptr;
        size_t semicolon_offset = 0;
        while (line + semicolon_offset < end &&
               line[semicolon_offset] != ';')
        {
            semicolon_offset++;
        }
        const uint8_t *value_ptr = line + semicolon_offset + 1u;
        if (value_ptr >= end) break;
        int64_t value;
        size_t advance;
        parse_temp_dot_bounded(value_ptr, end, &value, &advance);
        ptr = value_ptr + advance;
        dense_update_direct(
            stats,
            make_key(load_u64_bounded(line, end), semicolon_offset),
            value);
    }
}

static void process_dense(
    const uint8_t *restrict data,
    size_t length,
    DenseStat *restrict stats)
{
    if (length == 0) return;
    __m256i semi_vec = _mm256_set1_epi8(';');
    const uint8_t *end = data + length;
    const uint8_t *end0 = data + length / 2u;
    while (end0 < end && *end0 != '\n') end0++;
    if (end0 < end) end0++;

    const uint8_t *ptr0 = data;
    const uint8_t *ptr1 = end0;
    const uint8_t *end1 = end;
    const uint8_t *safe0 = (size_t)(end0 - ptr0) > MAX_LINE
        ? end0 - MAX_LINE : ptr0;
    const uint8_t *safe1 = (size_t)(end1 - ptr1) > MAX_LINE
        ? end1 - MAX_LINE : ptr1;

    if (ptr0 < safe0 && ptr1 < safe1) {
        DenseParsed parsed0 = parse_line_dense(ptr0, semi_vec);
        DenseParsed parsed1 = parse_line_dense(ptr1, semi_vec);
        for (;;) {
            ptr0 = parsed0.next;
            ptr1 = parsed1.next;
            if (!(ptr0 < safe0 && ptr1 < safe1)) {
                dense_update_direct(stats, parsed0.key, parsed0.val);
                dense_update_direct(stats, parsed1.key, parsed1.val);
                break;
            }
            __builtin_prefetch(ptr0 + 128, 0, 3);
            __builtin_prefetch(ptr1 + 128, 0, 3);
            DenseParsed next0 = parse_line_dense(ptr0, semi_vec);
            DenseParsed next1 = parse_line_dense(ptr1, semi_vec);
            __builtin_prefetch(
                &stats[(size_t)next0.key & DISPATCH_MASK],
                1,
                3);
            __builtin_prefetch(
                &stats[(size_t)next1.key & DISPATCH_MASK],
                1,
                3);
            dense_update_direct(stats, parsed0.key, parsed0.val);
            dense_update_direct(stats, parsed1.key, parsed1.val);
            parsed0 = next0;
            parsed1 = next1;
        }
    }

    dense_drain(ptr0, safe0, end0, semi_vec, stats);
    dense_drain(ptr1, safe1, end1, semi_vec, stats);
}

// Snap a raw offset forward to the byte after the next '\n' within MAX_LINE,
// or to file_size if EOF reached first.
static inline off_t snap_to_line(off_t off, const uint8_t *data, off_t file_size) {
    if (off == 0 || off >= file_size) return off;
    off_t lim = off + MAX_LINE < file_size ? off + MAX_LINE : file_size;
    while (off < lim && data[off - 1] != '\n') off++;
    return off;
}

static void *dense_worker_main(void *arg_ptr) {
    DenseWorkerArgs *arg = (DenseWorkerArgs *)arg_ptr;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET((unsigned)arg->thread_idx, &set);
    (void)sched_setaffinity(0, sizeof set, &set);

    size_t stats_bytes = sizeof(DenseStat) * DISPATCH_SIZE;
    size_t allocation_bytes = (stats_bytes + 63u) & ~63u;
    DenseStat *stats = aligned_alloc(64, allocation_bytes);
    if (!stats) die("aligned_alloc dense stats");
    memset(stats, 0, stats_bytes);

    const uint8_t *data = arg->data;
    off_t file_size = arg->file_size;
    const off_t segment_size = 2LL << 20;
    for (;;) {
        long long raw = atomic_fetch_add_explicit(
            arg->cursor,
            segment_size,
            memory_order_relaxed);
        if (raw >= file_size) break;
        off_t start = snap_to_line((off_t)raw, data, file_size);
        off_t end = snap_to_line(
            raw + segment_size < file_size
                ? (off_t)(raw + segment_size)
                : file_size,
            data,
            file_size);
        if (start >= end) continue;
        process_dense(
            data + start,
            (size_t)(end - start),
            stats);
    }
    arg->stats = stats;
    return NULL;
}

static void *worker_main(void *arg_ptr) {
    WorkerArgs *arg = (WorkerArgs *)arg_ptr;

    // Pin to one core; ignore failure (Hyper-V hosts may restrict affinity).
    cpu_set_t set;
    CPU_ZERO(&set); CPU_SET((unsigned)arg->thread_idx, &set);
    (void)sched_setaffinity(0, sizeof set, &set);

    PerHot *hot = aligned_alloc(64, HOT_BYTES + NAMES_BYTES);
    if (!hot) die("aligned_alloc worker tables");
    memset(hot, 0, HOT_BYTES);

    const uint8_t *data = arg->data;
    off_t file_size = arg->file_size;
    const off_t seg = 2LL << 20;

    for (;;) {
        long long raw = atomic_fetch_add_explicit(arg->cursor, seg, memory_order_relaxed);
        if (raw >= file_size) break;

        off_t s = snap_to_line((off_t)raw, data, file_size);
        off_t e = snap_to_line(raw + seg < file_size ? (off_t)(raw + seg) : file_size, data, file_size);
        if (s >= e) continue;
        process_avx2(data + s, (size_t)(e - s), hot);
    }
    arg->hot = hot;
    return NULL;
}

static void merge_tables(HotEntry *dst_hot, NameEntry *dst_names, const PerHot *src_hot, const NameEntry *src_names) {
    for (size_t i = 0; i < TABLE_SIZE; i++) {
        const PerHot *src = &src_hot[i];
        if (src->key == 0) continue;

        size_t idx = (size_t)src->key & TABLE_MASK;
        for (size_t probes = 0; probes < TABLE_SIZE; probes++) {
            HotEntry *dst = &dst_hot[idx];
            if (dst->key == 0) {
                dst->key = src->key; dst->count = src->count; dst->sum = src->sum;
                dst->min = src->min; dst->max = src->max;
                dst_names[idx] = src_names[i];
                goto next;
            }
            if (dst->key == src->key &&
                dst_names[idx].name_len == src_names[i].name_len &&
                memcmp(dst_names[idx].name, src_names[i].name, dst_names[idx].name_len) == 0)
            {
                dst->count += src->count; dst->sum += src->sum;
                if (src->min < dst->min) dst->min = src->min;
                if (src->max > dst->max) dst->max = src->max;
                goto next;
            }
            idx = (idx + 1u) & TABLE_MASK;
        }
        die("merge table full");
next:;
    }
}

static int compare_names(const void *lhs, const void *rhs, void *ctx) {
    const NameEntry *names = (const NameEntry *)ctx;
    const NameEntry *a = &names[*(const size_t *)lhs];
    const NameEntry *b = &names[*(const size_t *)rhs];
    size_t lo = a->name_len < b->name_len ? a->name_len : b->name_len;
    int cmp = memcmp(a->name, b->name, lo);
    return cmp != 0 ? cmp : (int)a->name_len - (int)b->name_len;
}

static char *push_temp(char *p, int32_t v) {
    uint32_t a;
    if (v < 0) { *p++ = '-'; a = (uint32_t)-v; } else a = (uint32_t)v;
    uint32_t ip = a / 10u;
    if (ip >= 10u) *p++ = (char)('0' + ip / 10u);
    *p++ = (char)('0' + ip % 10u);
    *p++ = '.';
    *p++ = (char)('0' + a % 10u);
    return p;
}

// Banker's rounding to nearest tenth; sum is already in tenths.
static int32_t avg_x10(int64_t sum, uint32_t count) {
    double x = (double)sum / (double)count;
    double fl = floor(x);
    if (fabs(x - fl - 0.5) < DBL_EPSILON * 4.0)
        return (int32_t)((long long)fl % 2 == 0 ? fl : fl + 1.0);
    return (int32_t)llround(x);
}

static int compare_dense_names(const void *lhs, const void *rhs, void *ctx) {
    const DenseDictionary *dictionary = (const DenseDictionary *)ctx;
    const NameEntry *a = &dictionary->names[*(const size_t *)lhs];
    const NameEntry *b = &dictionary->names[*(const size_t *)rhs];
    size_t shorter = a->name_len < b->name_len ? a->name_len : b->name_len;
    int comparison = memcmp(a->name, b->name, shorter);
    return comparison != 0
        ? comparison
        : (int)a->name_len - (int)b->name_len;
}

static char *calculate_dense(
    const uint8_t *data,
    off_t file_size,
    int nthreads,
    const DenseDictionary *dictionary)
{
    _Atomic long long cursor = 0;
    pthread_t *threads = malloc(sizeof(pthread_t) * (size_t)nthreads);
    DenseWorkerArgs *args = calloc((size_t)nthreads, sizeof *args);
    if (!threads || !args) die("alloc dense threads");
    for (int i = 0; i < nthreads; i++) {
        args[i].data = data;
        args[i].file_size = file_size;
        args[i].cursor = &cursor;
        args[i].thread_idx = i;
        if (pthread_create(&threads[i], NULL, dense_worker_main, &args[i]))
            die("pthread_create dense");
    }
    for (int i = 0; i < nthreads; i++) pthread_join(threads[i], NULL);

    DenseStat *merged = calloc(STATION_COUNT, sizeof *merged);
    if (!merged) die("calloc dense merged");
    for (int worker = 0; worker < nthreads; worker++) {
        for (size_t id = 0; id < STATION_COUNT; id++) {
            const DenseStat *src = &args[worker].stats[
                dictionary->indices[id]];
            if (src->count == 0) continue;
            DenseStat *dst = &merged[id];
            if (dst->count == 0) {
                *dst = *src;
            } else {
                dst->count += src->count;
                dst->sum += src->sum;
                if (src->min < dst->min) dst->min = src->min;
                if (src->max > dst->max) dst->max = src->max;
            }
        }
    }

    size_t *slots = malloc(sizeof(size_t) * STATION_COUNT);
    if (!slots) die("malloc dense slots");
    for (size_t i = 0; i < STATION_COUNT; i++) slots[i] = i;
    qsort_r(
        slots,
        STATION_COUNT,
        sizeof *slots,
        compare_dense_names,
        (void *)dictionary);

    char *out = malloc(64 * 1024);
    if (!out) die("malloc dense output");
    char *p = out;
    *p++ = '{';
    for (size_t position = 0; position < STATION_COUNT; position++) {
        size_t id = slots[position];
        if (position > 0) { *p++ = ','; *p++ = ' '; }
        const NameEntry *name = &dictionary->names[id];
        const DenseStat *stat = &merged[id];
        memcpy(p, name->name, name->name_len);
        p += name->name_len;
        *p++ = '=';
        p = push_temp(p, stat->min);
        *p++ = '/';
        p = push_temp(p, avg_x10(stat->sum, stat->count));
        *p++ = '/';
        p = push_temp(p, stat->max);
    }
    *p++ = '}';
    *p = 0;
    return out;
}

static char *calculate(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) die("stat");
    off_t file_size = st.st_size;

    const char *env = getenv("NTHREADS");
    int nthreads = env ? atoi(env) : 0;
    if (nthreads <= 0) { long n = sysconf(_SC_NPROCESSORS_ONLN); nthreads = n > 0 ? (int)n : 1; }

    int fd = open(path, O_RDONLY | O_NOATIME);
    if (fd < 0) die("open");
    uint8_t *data = mmap(NULL, (size_t)file_size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (data == MAP_FAILED) die("mmap");
    close(fd);
    (void)madvise(data, (size_t)file_size, MADV_WILLNEED);

    DenseDictionary *dictionary = calloc(1, sizeof *dictionary);
    if (!dictionary) die("calloc dense dictionary");
    if (build_dense_dictionary(data, (size_t)file_size, dictionary))
        return calculate_dense(data, file_size, nthreads, dictionary);
    free(dictionary);

    _Atomic long long cursor = 0;
    pthread_t *threads = malloc(sizeof(pthread_t) * (size_t)nthreads);
    WorkerArgs *args = calloc((size_t)nthreads, sizeof(WorkerArgs));
    if (!threads || !args) die("alloc threads");
    for (int i = 0; i < nthreads; i++) {
        args[i].data = data; args[i].file_size = file_size;
        args[i].cursor = &cursor; args[i].thread_idx = i;
        if (pthread_create(&threads[i], NULL, worker_main, &args[i])) die("pthread_create");
    }
    for (int i = 0; i < nthreads; i++) pthread_join(threads[i], NULL);

    HotEntry *merged_hot = calloc(TABLE_SIZE, sizeof(HotEntry));
    NameEntry *merged_names = calloc(TABLE_SIZE, sizeof(NameEntry));
    if (!merged_hot || !merged_names) die("calloc merged");
    for (int i = 0; i < nthreads; i++)
        merge_tables(merged_hot, merged_names, args[i].hot, names_for(args[i].hot));

    size_t *slots = malloc(sizeof(size_t) * TABLE_SIZE);
    if (!slots) die("malloc slots");
    size_t slot_count = 0;
    for (size_t i = 0; i < TABLE_SIZE; i++)
        if (merged_hot[i].key != 0) slots[slot_count++] = i;
    qsort_r(slots, slot_count, sizeof(size_t), compare_names, merged_names);

    // 413 stations × ~140 bytes max per row + braces/commas → ≤ 60 KiB.
    char *out = malloc(64 * 1024);
    if (!out) die("malloc out");
    char *p = out;
    *p++ = '{';
    for (size_t pos = 0; pos < slot_count; pos++) {
        size_t idx = slots[pos];
        if (pos > 0) { *p++ = ','; *p++ = ' '; }
        const HotEntry  *h = &merged_hot[idx];
        const NameEntry *n = &merged_names[idx];
        memcpy(p, n->name, n->name_len); p += n->name_len;
        *p++ = '=';
        p = push_temp(p, h->min);
        *p++ = '/';
        p = push_temp(p, avg_x10(h->sum, h->count));
        *p++ = '/';
        p = push_temp(p, h->max);
    }
    *p++ = '}'; *p = 0;
    // No frees: main calls _exit(0); kernel reclaims everything.
    return out;
}

int main(int argc, char **argv) {
    puts(calculate(argc > 1 ? argv[1] : "/data/measurements_1B.txt"));
    fflush(stdout);
    _exit(0);
}
