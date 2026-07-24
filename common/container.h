/* Copyright (C) 2020 <KichikuouChrome@gmail.com>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
 *
*/
#ifndef COMMON_CONTAINER_H
#define COMMON_CONTAINER_H

#include <stdint.h>

typedef struct {
	void **data;
	int len;
	int cap;
} Vector;

Vector *new_vec(void);
void vec_push(Vector *v, void *e);
void vec_set(Vector *v, int index, void *e);

void stack_push(Vector *stack, uintptr_t n);
void stack_pop(Vector *stack);
uintptr_t stack_top(Vector *stack);

typedef struct {
	Vector *keys;
	Vector *vals;
} Map;

Map *new_map(void);
void map_put(Map *m, const char *key, void *val);
void *map_get(Map *m, const char *key);

typedef struct {
	const void *key;
	void *val;
} HashItem;

typedef uint32_t (*HashFunc)(const void *key);
// Returns zero if the two keys are equal.
typedef int (*HashKeyCompare)(const void *k1, const void *k2);

typedef struct {
	HashItem *table;
	uint32_t size;
	uint32_t occupied;
	HashFunc hash;
	HashKeyCompare compare;
} HashMap;

HashMap *new_hash(HashFunc hash, HashKeyCompare compare);
HashMap *new_string_hash(void);
void hash_put(HashMap *m, const void *key, const void *val);
void *hash_get(HashMap *m, const void *key);
HashItem *hash_iterate(HashMap *m, HashItem *item);

#endif
