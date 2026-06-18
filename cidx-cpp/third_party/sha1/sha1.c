/*
 * sha1.c — Public-domain SHA-1 implementation.
 *
 * Based on the reference implementation in RFC 3174 (D. J. Eastlake, 3rd
 * and Paul E. Jones, September 2001).  That code was released without
 * copyright restriction; this version is reformatted and placed in the
 * public domain.  No warranty.
 *
 * Produces output identical to Python hashlib.sha1 / OpenSSL SHA1.
 */
#include "sha1.h"

#include <string.h>

/* Rotate left 32-bit value n bits. */
#define ROL32(v, n) (((v) << (n)) | ((v) >> (32 - (n))))

/* SHA-1 compression constants and functions (FIPS PUB 180-1 §6.1). */
#define K0 0x5A827999u
#define K1 0x6ED9EBA1u
#define K2 0x8F1BBCDCu
#define K3 0xCA62C1D6u

#define F0(b, c, d) (((b) & (c)) | (~(b) & (d)))
#define F1(b, c, d) ((b) ^ (c) ^ (d))
#define F2(b, c, d) (((b) & (c)) | ((b) & (d)) | ((c) & (d)))
#define F3(b, c, d) ((b) ^ (c) ^ (d))

static void sha1_transform(SHA1_CTX *ctx, const unsigned char block[64]) {
  uint32_t W[80];
  uint32_t a, b, c, d, e, tmp;
  int i;

  /* Prepare message schedule W. */
  for (i = 0; i < 16; ++i) {
    W[i] = ((uint32_t)block[i * 4 + 0] << 24) |
            ((uint32_t)block[i * 4 + 1] << 16) |
            ((uint32_t)block[i * 4 + 2] <<  8) |
            ((uint32_t)block[i * 4 + 3]);
  }
  for (i = 16; i < 80; ++i) {
    W[i] = ROL32(W[i-3] ^ W[i-8] ^ W[i-14] ^ W[i-16], 1);
  }

  a = ctx->state[0];
  b = ctx->state[1];
  c = ctx->state[2];
  d = ctx->state[3];
  e = ctx->state[4];

  for (i = 0; i < 20; ++i) {
    tmp = ROL32(a, 5) + F0(b, c, d) + e + W[i] + K0;
    e = d; d = c; c = ROL32(b, 30); b = a; a = tmp;
  }
  for (i = 20; i < 40; ++i) {
    tmp = ROL32(a, 5) + F1(b, c, d) + e + W[i] + K1;
    e = d; d = c; c = ROL32(b, 30); b = a; a = tmp;
  }
  for (i = 40; i < 60; ++i) {
    tmp = ROL32(a, 5) + F2(b, c, d) + e + W[i] + K2;
    e = d; d = c; c = ROL32(b, 30); b = a; a = tmp;
  }
  for (i = 60; i < 80; ++i) {
    tmp = ROL32(a, 5) + F3(b, c, d) + e + W[i] + K3;
    e = d; d = c; c = ROL32(b, 30); b = a; a = tmp;
  }

  ctx->state[0] += a;
  ctx->state[1] += b;
  ctx->state[2] += c;
  ctx->state[3] += d;
  ctx->state[4] += e;
}

void SHA1_Init(SHA1_CTX *ctx) {
  ctx->state[0] = 0x67452301u;
  ctx->state[1] = 0xEFCDAB89u;
  ctx->state[2] = 0x98BADCFEu;
  ctx->state[3] = 0x10325476u;
  ctx->state[4] = 0xC3D2E1F0u;
  ctx->count = 0;
  ctx->buf_len = 0;
}

void SHA1_Update(SHA1_CTX *ctx, const void *data, unsigned long len) {
  const unsigned char *p = (const unsigned char *)data;

  while (len > 0) {
    unsigned space = SHA1_BLOCK_SIZE - ctx->buf_len;
    unsigned take = len < space ? (unsigned)len : space;
    memcpy(ctx->buffer + ctx->buf_len, p, take);
    ctx->buf_len += take;
    p += take;
    len -= take;
    ctx->count += (uint64_t)take * 8;
    if (ctx->buf_len == SHA1_BLOCK_SIZE) {
      sha1_transform(ctx, ctx->buffer);
      ctx->buf_len = 0;
    }
  }
}

void SHA1_Final(unsigned char digest[SHA1_DIGEST_SIZE], SHA1_CTX *ctx) {
  /* Append the 0x80 padding byte. */
  ctx->buffer[ctx->buf_len++] = 0x80;

  /* If not enough room for the 8-byte length, pad and compress. */
  if (ctx->buf_len > 56) {
    memset(ctx->buffer + ctx->buf_len, 0, SHA1_BLOCK_SIZE - ctx->buf_len);
    sha1_transform(ctx, ctx->buffer);
    ctx->buf_len = 0;
  }
  memset(ctx->buffer + ctx->buf_len, 0, 56 - ctx->buf_len);

  /* Append message bit length (big-endian 64-bit). */
  uint64_t bits = ctx->count;
  ctx->buffer[56] = (unsigned char)(bits >> 56);
  ctx->buffer[57] = (unsigned char)(bits >> 48);
  ctx->buffer[58] = (unsigned char)(bits >> 40);
  ctx->buffer[59] = (unsigned char)(bits >> 32);
  ctx->buffer[60] = (unsigned char)(bits >> 24);
  ctx->buffer[61] = (unsigned char)(bits >> 16);
  ctx->buffer[62] = (unsigned char)(bits >>  8);
  ctx->buffer[63] = (unsigned char)(bits);
  sha1_transform(ctx, ctx->buffer);

  /* Store digest in big-endian byte order. */
  int i;
  for (i = 0; i < 5; ++i) {
    digest[i * 4 + 0] = (unsigned char)(ctx->state[i] >> 24);
    digest[i * 4 + 1] = (unsigned char)(ctx->state[i] >> 16);
    digest[i * 4 + 2] = (unsigned char)(ctx->state[i] >>  8);
    digest[i * 4 + 3] = (unsigned char)(ctx->state[i]);
  }
}
