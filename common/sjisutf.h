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
#ifndef COMMON_SJISUTF_H
#define COMMON_SJISUTF_H

#include <stdbool.h>
#include <stdint.h>

#define sjis2utf(s) sjis2utf_sub((s), -1)
#define utf2sjis(s) utf2sjis_sub((s), -1)
char *sjis2utf_sub(const char *str, int substitution_char);
char *utf2sjis_sub(const char *str, int substitution_char);
uint8_t compact_sjis(uint8_t c1, uint8_t c2);
uint16_t expand_sjis(uint8_t c);
bool is_valid_sjis(uint8_t c1, uint8_t c2);
bool is_unicode_safe(uint8_t c1, uint8_t c2);

// Returns NULL if s is a valid UTF-8 string. Otherwise, returns the first
// invalid character.
const char *validate_utf8(const char *s);

static inline bool is_sjis_half_kana(uint8_t c) {
	return 0xa1 <= c && c <= 0xdf;
}

static inline bool is_compacted_sjis(uint8_t c) {
	return c == ' ' || (0xa1 <= c && c <= 0xdd);
}

static inline bool is_sjis_byte1(uint8_t c) {
	return (0x81 <= c && c <= 0x9f) || (0xe0 <= c && c <= 0xfc);
}

static inline bool is_sjis_byte2(uint8_t c) {
	return 0x40 <= c && c <= 0xfc && c != 0x7f;
}

#define UTF8_TRAIL_BYTE(b) ((int8_t)(b) < -0x40)

#endif
