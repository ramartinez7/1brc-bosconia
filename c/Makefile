CC ?= cc
CFLAGS ?= -O3 -flto -march=native -mavx2 -mbmi2 -std=c11 -Wall -Wextra -Wpedantic -pthread
LDFLAGS ?= -flto -pthread -lm
TARGET := c-linux

.PHONY: all clean

all: $(TARGET)

$(TARGET): main.c
	$(CC) $(CFLAGS) main.c -o $(TARGET) $(LDFLAGS)

clean:
	rm -f $(TARGET)
