#define main onebrc_program_main
#include "../c/main.c"
#undef main

static uint8_t *read_file(const char *path, size_t *size) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    if (fseek(file, 0, SEEK_END) != 0) return NULL;
    long length = ftell(file);
    if (length < 0 || fseek(file, 0, SEEK_SET) != 0) return NULL;
    uint8_t *data = malloc((size_t)length);
    if (!data || fread(data, 1, (size_t)length, file) != (size_t)length)
        return NULL;
    fclose(file);
    *size = (size_t)length;
    return data;
}

static uint8_t *read_guarded_file(
    const char *path,
    size_t *size,
    size_t *mapping_size)
{
    size_t length = 0;
    uint8_t *source = read_file(path, &length);
    long page_size = sysconf(_SC_PAGESIZE);
    if (!source || page_size <= 0 || length % (size_t)page_size != 0)
        return NULL;

    size_t total = length + (size_t)page_size;
    uint8_t *mapping = mmap(
        NULL,
        total,
        PROT_NONE,
        MAP_PRIVATE | MAP_ANONYMOUS,
        -1,
        0);
    if (mapping == MAP_FAILED ||
        mprotect(mapping, length, PROT_READ | PROT_WRITE) != 0)
        return NULL;
    memcpy(mapping, source, length);
    free(source);
    if (mprotect(mapping, length, PROT_READ) != 0) return NULL;
    *size = length;
    *mapping_size = total;
    return mapping;
}

static int verify_guarded_generic(
    const char *path,
    uint64_t expected_count,
    int64_t expected_sum,
    size_t expected_entries)
{
    size_t size = 0;
    size_t mapping_size = 0;
    uint8_t *data = read_guarded_file(path, &size, &mapping_size);
    PerHot *hot = aligned_alloc(64, HOT_BYTES + NAMES_BYTES);
    if (!data || !hot) return 0;
    memset(hot, 0, HOT_BYTES);
    process_avx2(data, size, hot);

    uint64_t count = 0;
    int64_t sum = 0;
    size_t entries = 0;
    for (size_t i = 0; i < TABLE_SIZE; i++) {
        if (hot[i].count == 0) continue;
        count += hot[i].count;
        sum += hot[i].sum;
        entries++;
    }
    munmap(data, mapping_size);
    free(hot);
    return count == expected_count && sum == expected_sum &&
        entries == expected_entries;
}

int main(int argc, char **argv) {
    if (argc != 8) return 2;

    char names[STATION_COUNT][8] = {{0}};
    uint8_t used[DISPATCH_SIZE] = {0};
    size_t count = 0;
    for (unsigned candidate = 0;
         candidate < 1000000 && count < STATION_COUNT;
         candidate++)
    {
        char name[8];
        snprintf(name, sizeof name, "S%06u", candidate);
        uint64_t first8 = 0;
        memcpy(&first8, name, 7);
        uint32_t index = make_key(first8, 7) & DISPATCH_MASK;
        if (used[index]) continue;
        used[index] = 1;
        memcpy(names[count++], name, 8);
    }
    if (count != STATION_COUNT) return 3;

    size_t capacity = STATION_COUNT * 16u;
    uint8_t *data = malloc(capacity);
    if (!data) return 4;
    size_t length = 0;
    for (size_t i = 0; i < STATION_COUNT; i++) {
        int written = snprintf(
            (char *)data + length,
            capacity - length,
            "%s;1.0\n",
            names[i]);
        if (written <= 0) return 5;
        length += (size_t)written;
    }

    DenseDictionary *dictionary = calloc(1, sizeof *dictionary);
    if (!dictionary || !build_dense_dictionary(data, length, dictionary))
        return 6;
    char *actual = calculate_dense(data, (off_t)length, 4, dictionary);

    char *expected = malloc(64 * 1024);
    if (!expected) return 7;
    char *out = expected;
    static const char stats[] = "=1.0/1.0/1.0";
    *out++ = '{';
    for (size_t i = 0; i < STATION_COUNT; i++) {
        if (i > 0) { *out++ = ','; *out++ = ' '; }
        memcpy(out, names[i], 7);
        out += 7;
        memcpy(out, stats, sizeof stats - 1u);
        out += sizeof stats - 1u;
    }
    *out++ = '}';
    *out = '\0';
    if (strcmp(actual, expected) != 0) {
        fputs("dense output mismatch\n", stderr);
        return 8;
    }

    size_t collision_size = 0;
    uint8_t *collision_data = read_file(argv[1], &collision_size);
    DenseDictionary *collision_dictionary = calloc(
        1,
        sizeof *collision_dictionary);
    if (!collision_data || !collision_dictionary) return 9;
    if (build_dense_dictionary(
            collision_data,
            collision_size,
            collision_dictionary))
    {
        fputs("collision fixture unexpectedly selected dense mode\n", stderr);
        return 10;
    }

    size_t dense_page_size = 0;
    size_t dense_mapping_size = 0;
    uint8_t *dense_page = read_guarded_file(
        argv[2],
        &dense_page_size,
        &dense_mapping_size);
    DenseStat *page_stats = calloc(DISPATCH_SIZE, sizeof *page_stats);
    if (!dense_page || !page_stats) return 11;
    process_dense(dense_page, dense_page_size, page_stats);
    uint64_t dense_count = 0;
    int64_t dense_sum = 0;
    size_t dense_entries = 0;
    for (size_t i = 0; i < DISPATCH_SIZE; i++) {
        if (page_stats[i].count == 0) continue;
        dense_count += page_stats[i].count;
        dense_sum += page_stats[i].sum;
        dense_entries++;
    }
    if (dense_count != 953 || dense_sum != -999 || dense_entries != 413)
        return 12;

    if (!verify_guarded_generic(argv[3], 410, 4090, 2))
        return 14;
    if (!verify_guarded_generic(argv[4], 403, 3990, 4))
        return 15;
    if (!verify_guarded_generic(argv[5], 406, 4050, 2))
        return 16;
    if (!verify_guarded_generic(argv[6], 398, 3950, 3))
        return 17;
    if (!verify_guarded_generic(argv[7], 404, 4020, 3))
        return 18;

    munmap(dense_page, dense_mapping_size);
    free(page_stats);
    free(collision_dictionary);
    free(collision_data);
    free(expected);
    free(data);
    free(dictionary);
    puts("dense execution, collision fallback, and guarded EOF: PASS");
    return 0;
}
