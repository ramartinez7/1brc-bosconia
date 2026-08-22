// Guarded corpus harness: every case is copied so that its last byte abuts an
// inaccessible PROT_NONE page, which turns any read past end-of-input into an
// immediate fault instead of a silent read of adjacent memory.
//
// Usage: parser-fuzz-harness <guarded.tsv>
// Rows:  <relative path>\t<mode>\t<count>\t<sum>\t<entries>\t<min_sum>\t<max_sum>
// Modes: generic     the dense dictionary must decline and process_avx2 must
//                    reproduce the expected aggregates;
//        dense       the dense dictionary must engage and both paths must
//                    reproduce the expected aggregates;
//        unspecified out-of-contract input: results are not a contract, only
//                    staying inside the mapping is.

#ifndef ONEBRC_SOURCE
#define ONEBRC_SOURCE "../c/main.c"
#endif

#define main onebrc_program_main
#include ONEBRC_SOURCE
#undef main

#include <inttypes.h>

#define FUZZ_FIELDS 7
#define FUZZ_LINE 4096

typedef struct {
    uint64_t count;
    int64_t  sum;
    uint64_t entries;
    int64_t  min_sum;
    int64_t  max_sum;
} Aggregate;

typedef struct {
    uint8_t       *mapping;
    size_t         mapping_size;
    const uint8_t *data;
    size_t         size;
} Guarded;

static size_t page_bytes(void) {
    long size = sysconf(_SC_PAGESIZE);
    return size > 0 ? (size_t)size : 4096u;
}

static uint8_t *read_file(const char *path, size_t *size) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    if (fseek(file, 0, SEEK_END) != 0) { fclose(file); return NULL; }
    long length = ftell(file);
    if (length < 0 || fseek(file, 0, SEEK_SET) != 0) { fclose(file); return NULL; }
    *size = (size_t)length;
    uint8_t *data = malloc((size_t)length + 1u);
    if (!data) { fclose(file); return NULL; }
    if (length > 0 && fread(data, 1, (size_t)length, file) != (size_t)length) {
        free(data);
        fclose(file);
        return NULL;
    }
    fclose(file);
    return data;
}

// The input is placed flush against the guard page, so the mapping ends
// exactly at end-of-input whatever the file size is.
static int map_guarded(const char *path, Guarded *out) {
    size_t size = 0;
    uint8_t *contents = read_file(path, &size);
    if (!contents) return 0;

    size_t page = page_bytes();
    size_t usable = ((size + page - 1u) / page) * page;
    size_t total = usable + page;
    uint8_t *mapping = mmap(
        NULL,
        total,
        PROT_NONE,
        MAP_PRIVATE | MAP_ANONYMOUS,
        -1,
        0);
    if (mapping == MAP_FAILED) { free(contents); return 0; }
    if (usable > 0) {
        if (mprotect(mapping, usable, PROT_READ | PROT_WRITE) != 0) {
            free(contents);
            munmap(mapping, total);
            return 0;
        }
        memcpy(mapping + usable - size, contents, size);
        if (mprotect(mapping, usable, PROT_READ) != 0) {
            free(contents);
            munmap(mapping, total);
            return 0;
        }
    }
    free(contents);
    out->mapping = mapping;
    out->mapping_size = total;
    out->data = mapping + usable - size;
    out->size = size;
    return 1;
}

static int aggregate_generic(const uint8_t *data, size_t size, Aggregate *out) {
    PerHot *hot = aligned_alloc(64, HOT_BYTES + NAMES_BYTES);
    if (!hot) return 0;
    memset(hot, 0, HOT_BYTES + NAMES_BYTES);
    process_avx2(data, size, hot);

    Aggregate totals = {0, 0, 0, 0, 0};
    for (size_t i = 0; i < TABLE_SIZE; i++) {
        if (hot[i].count == 0) continue;
        totals.count += hot[i].count;
        totals.sum += hot[i].sum;
        totals.entries++;
        totals.min_sum += hot[i].min;
        totals.max_sum += hot[i].max;
    }
    free(hot);
    *out = totals;
    return 1;
}

static int aggregate_dense(const uint8_t *data, size_t size, Aggregate *out) {
    // process_dense requires sentinel-initialized slots: count 0 marks an
    // untouched slot and the reversed min/max extremes let the first value win.
    DenseStat *stats = aligned_alloc(64, sizeof *stats * DISPATCH_SIZE);
    if (!stats) return 0;
    for (size_t i = 0; i < DISPATCH_SIZE; i++) {
        stats[i].count = 0;
        stats[i].min = INT16_MAX;
        stats[i].max = INT16_MIN;
        stats[i].sum = 0;
    }
    process_dense(data, size, stats);

    Aggregate totals = {0, 0, 0, 0, 0};
    for (size_t i = 0; i < DISPATCH_SIZE; i++) {
        if (stats[i].count == 0) continue;
        totals.count += stats[i].count;
        totals.sum += stats[i].sum;
        totals.entries++;
        totals.min_sum += stats[i].min;
        totals.max_sum += stats[i].max;
    }
    free(stats);
    *out = totals;
    return 1;
}

static int same_aggregate(const Aggregate *actual, const Aggregate *expected) {
    return actual->count == expected->count &&
        actual->sum == expected->sum &&
        actual->entries == expected->entries &&
        actual->min_sum == expected->min_sum &&
        actual->max_sum == expected->max_sum;
}

static void report(const char *path, const char *mode, const char *what,
                   const Aggregate *actual, const Aggregate *expected) {
    fprintf(stderr, "parser-fuzz-harness: FAIL: %s [%s] %s\n", path, mode, what);
    if (actual && expected) {
        fprintf(
            stderr,
            "  expected count=%" PRIu64 " sum=%" PRId64 " entries=%" PRIu64
            " min_sum=%" PRId64 " max_sum=%" PRId64 "\n"
            "  actual   count=%" PRIu64 " sum=%" PRId64 " entries=%" PRIu64
            " min_sum=%" PRId64 " max_sum=%" PRId64 "\n",
            expected->count, expected->sum, expected->entries,
            expected->min_sum, expected->max_sum,
            actual->count, actual->sum, actual->entries,
            actual->min_sum, actual->max_sum);
    }
}

static int run_case(const char *path, const char *mode, const Aggregate *expected) {
    Guarded guarded;
    if (!map_guarded(path, &guarded)) {
        fprintf(stderr, "parser-fuzz-harness: cannot map %s\n", path);
        return 0;
    }

    int ok = 1;
    DenseDictionary *dictionary = calloc(1, sizeof *dictionary);
    if (!dictionary) {
        munmap(guarded.mapping, guarded.mapping_size);
        return 0;
    }
    int dense_selected = build_dense_dictionary(
        guarded.data,
        guarded.size,
        dictionary);

    Aggregate generic = {0, 0, 0, 0, 0};
    if (!aggregate_generic(guarded.data, guarded.size, &generic)) ok = 0;

    if (ok && strcmp(mode, "unspecified") == 0) {
        Aggregate ignored = {0, 0, 0, 0, 0};
        if (!aggregate_dense(guarded.data, guarded.size, &ignored)) ok = 0;
    } else if (ok && strcmp(mode, "dense") == 0) {
        if (!dense_selected) {
            report(path, mode, "dense dictionary declined", NULL, NULL);
            ok = 0;
        } else {
            Aggregate dense = {0, 0, 0, 0, 0};
            if (!aggregate_dense(guarded.data, guarded.size, &dense)) ok = 0;
            else if (!same_aggregate(&dense, expected)) {
                report(path, mode, "dense aggregates differ", &dense, expected);
                ok = 0;
            }
        }
        if (ok && !same_aggregate(&generic, expected)) {
            report(path, mode, "generic aggregates differ", &generic, expected);
            ok = 0;
        }
    } else if (ok) {
        if (dense_selected) {
            report(path, mode, "dense dictionary engaged unexpectedly", NULL, NULL);
            ok = 0;
        } else if (!same_aggregate(&generic, expected)) {
            report(path, mode, "generic aggregates differ", &generic, expected);
            ok = 0;
        }
    }

    free(dictionary);
    munmap(guarded.mapping, guarded.mapping_size);
    return ok;
}

static int split_fields(char *line, char *fields[FUZZ_FIELDS]) {
    size_t count = 0;
    char *cursor = line;
    while (count < FUZZ_FIELDS) {
        fields[count++] = cursor;
        char *tab = strchr(cursor, '\t');
        if (!tab) break;
        *tab = '\0';
        cursor = tab + 1;
    }
    return count == FUZZ_FIELDS && strchr(cursor, '\t') == NULL;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <guarded.tsv>\n", argv[0]);
        return 2;
    }

    char directory[FUZZ_LINE];
    snprintf(directory, sizeof directory, "%s", argv[1]);
    char *slash = strrchr(directory, '/');
    if (slash) *slash = '\0'; else snprintf(directory, sizeof directory, ".");

    FILE *manifest = fopen(argv[1], "r");
    if (!manifest) {
        fprintf(stderr, "parser-fuzz-harness: cannot open %s\n", argv[1]);
        return 2;
    }

    size_t total = 0;
    size_t unspecified = 0;
    char line[FUZZ_LINE];
    while (fgets(line, sizeof line, manifest)) {
        size_t length = strlen(line);
        while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r'))
            line[--length] = '\0';
        if (length == 0) continue;

        char *fields[FUZZ_FIELDS];
        if (!split_fields(line, fields)) {
            fprintf(stderr, "parser-fuzz-harness: malformed manifest row\n");
            fclose(manifest);
            return 2;
        }

        char path[FUZZ_LINE * 2 + 2];
        snprintf(path, sizeof path, "%s/%s", directory, fields[0]);
        // Announced before the run so a fault identifies its case.
        fprintf(stderr, "case: %s\n", fields[0]);
        fflush(stderr);
        Aggregate expected = {
            strtoull(fields[2], NULL, 10),
            strtoll(fields[3], NULL, 10),
            strtoull(fields[4], NULL, 10),
            strtoll(fields[5], NULL, 10),
            strtoll(fields[6], NULL, 10),
        };
        if (!run_case(path, fields[1], &expected)) {
            fclose(manifest);
            return 1;
        }
        total++;
        if (strcmp(fields[1], "unspecified") == 0) unspecified++;
    }
    fclose(manifest);

    printf(
        "guarded corpus: %zu cases (%zu in contract, %zu out of contract): PASS\n",
        total,
        total - unspecified,
        unspecified);
    return 0;
}
