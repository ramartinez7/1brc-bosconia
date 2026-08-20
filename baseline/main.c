#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TABLE_SIZE 32768u
#define TABLE_MASK (TABLE_SIZE - 1u)

typedef struct {
    char *name;
    size_t name_len;
    uint64_t hash;
    uint64_t count;
    int64_t sum;
    int32_t min;
    int32_t max;
} Entry;

static void die_errno(const char *operation) {
    perror(operation);
    exit(EXIT_FAILURE);
}

static void die(const char *message) {
    fprintf(stderr, "%s\n", message);
    exit(EXIT_FAILURE);
}

static uint64_t hash_name(const char *name, size_t length) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < length; i++) {
        hash ^= (unsigned char)name[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static int32_t parse_temperature(const char *text) {
    const char *ptr = text;
    int32_t sign = 1;
    if (*ptr == '-') {
        sign = -1;
        ptr++;
    }
    if (*ptr < '0' || *ptr > '9') die("invalid temperature");
    int32_t whole = *ptr++ - '0';
    if (*ptr != '.') {
        if (*ptr < '0' || *ptr > '9') die("invalid temperature");
        whole = whole * 10 + (*ptr++ - '0');
    }
    if (*ptr++ != '.' ||
        ptr[0] < '0' ||
        ptr[0] > '9' ||
        ptr[1] != '\0')
    {
        die("invalid temperature");
    }
    return sign * (whole * 10 + (ptr[0] - '0'));
}

static int32_t round_half_even(int64_t sum, uint64_t count) {
    uint64_t magnitude = sum < 0 ? (uint64_t)(-sum) : (uint64_t)sum;
    uint64_t quotient = magnitude / count;
    uint64_t remainder = magnitude % count;
    uint64_t doubled = remainder * 2u;
    if (doubled > count ||
        (doubled == count && (quotient & 1u) != 0))
    {
        quotient++;
    }
    int32_t rounded = (int32_t)quotient;
    return sum < 0 ? -rounded : rounded;
}

static Entry *find_entry(
    Entry *table,
    const char *name,
    size_t name_len,
    uint64_t hash)
{
    size_t index = (size_t)hash & TABLE_MASK;
    for (size_t probes = 0; probes < TABLE_SIZE; probes++) {
        Entry *entry = &table[index];
        if (entry->name == NULL) return entry;
        if (entry->hash == hash &&
            entry->name_len == name_len &&
            memcmp(entry->name, name, name_len) == 0)
        {
            return entry;
        }
        index = (index + 1u) & TABLE_MASK;
    }
    die("baseline table full");
    return NULL;
}

static int compare_entries(const void *lhs, const void *rhs) {
    const Entry *a = *(const Entry *const *)lhs;
    const Entry *b = *(const Entry *const *)rhs;
    size_t shorter = a->name_len < b->name_len
        ? a->name_len
        : b->name_len;
    int comparison = memcmp(a->name, b->name, shorter);
    return comparison != 0
        ? comparison
        : (a->name_len > b->name_len) - (a->name_len < b->name_len);
}

static void print_temperature(int32_t value) {
    uint32_t magnitude;
    if (value < 0) {
        putchar('-');
        magnitude = (uint32_t)-value;
    } else {
        magnitude = (uint32_t)value;
    }
    printf("%" PRIu32 ".%" PRIu32, magnitude / 10u, magnitude % 10u);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <measurements.txt>\n", argv[0]);
        return EXIT_FAILURE;
    }

    FILE *input = fopen(argv[1], "rb");
    if (input == NULL) die_errno("fopen");
    if (setvbuf(input, NULL, _IOFBF, 1u << 20) != 0)
        die_errno("setvbuf");

    Entry *table = calloc(TABLE_SIZE, sizeof *table);
    Entry **used = malloc(TABLE_SIZE * sizeof *used);
    if (table == NULL || used == NULL) die_errno("allocate baseline table");

    char *line = NULL;
    size_t capacity = 0;
    size_t used_count = 0;
    ssize_t length;
    while ((length = getline(&line, &capacity, input)) >= 0) {
        if (length > 0 && line[length - 1] == '\n')
            line[--length] = '\0';

        char *separator = memchr(line, ';', (size_t)length);
        if (separator == NULL ||
            separator == line ||
            memchr(
                separator + 1,
                ';',
                (size_t)(line + length - separator - 1)) != NULL)
        {
            die("invalid record");
        }

        size_t name_len = (size_t)(separator - line);
        int32_t value = parse_temperature(separator + 1);
        uint64_t hash = hash_name(line, name_len);
        Entry *entry = find_entry(table, line, name_len, hash);
        if (entry->name == NULL) {
            entry->name = malloc(name_len + 1u);
            if (entry->name == NULL) die_errno("allocate station name");
            memcpy(entry->name, line, name_len);
            entry->name[name_len] = '\0';
            entry->name_len = name_len;
            entry->hash = hash;
            entry->count = 1;
            entry->sum = value;
            entry->min = value;
            entry->max = value;
            used[used_count++] = entry;
        } else {
            entry->count++;
            entry->sum += value;
            if (value < entry->min) entry->min = value;
            if (value > entry->max) entry->max = value;
        }
    }
    if (ferror(input)) die_errno("getline");
    fclose(input);
    free(line);

    qsort(used, used_count, sizeof *used, compare_entries);
    putchar('{');
    for (size_t i = 0; i < used_count; i++) {
        Entry *entry = used[i];
        if (i != 0) fputs(", ", stdout);
        fwrite(entry->name, 1, entry->name_len, stdout);
        putchar('=');
        print_temperature(entry->min);
        putchar('/');
        print_temperature(round_half_even(entry->sum, entry->count));
        putchar('/');
        print_temperature(entry->max);
    }
    puts("}");

    for (size_t i = 0; i < used_count; i++) free(used[i]->name);
    free(used);
    free(table);
    return EXIT_SUCCESS;
}
