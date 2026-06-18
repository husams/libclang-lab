/*
 * sha1.h — Public-domain SHA-1 implementation (RFC 3174).
 *
 * Derived from the public-domain implementation by D. J. Eastlake, 3rd
 * and Paul E. Jones (RFC 3174, September 2001).
 *
 * Placed in the public domain.  No warranty.
 */
#ifndef CIDX_SHA1_H
#define CIDX_SHA1_H

#include <stdint.h>

#define SHA1_DIGEST_SIZE 20
#define SHA1_BLOCK_SIZE  64

typedef struct {
  uint32_t state[5];
  uint64_t count; /* bits processed */
  unsigned char buffer[SHA1_BLOCK_SIZE];
  unsigned buf_len;
} SHA1_CTX;

void SHA1_Init(SHA1_CTX *ctx);
void SHA1_Update(SHA1_CTX *ctx, const void *data, unsigned long len);
void SHA1_Final(unsigned char digest[SHA1_DIGEST_SIZE], SHA1_CTX *ctx);

#endif /* CIDX_SHA1_H */
