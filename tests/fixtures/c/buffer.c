#include "buffer.h"

typedef struct Buffer {
    char *data;
    int len;
} Buffer;

/* Append bytes to a buffer. */
static int buffer_append(Buffer *b, const char *src, int n) {
    return n;
}

enum Mode { READ, WRITE };
