/*
 * Copyright © 2019 Keith Packard <keithp@keithp.com>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * General Public License for more details.
 */

#include "snek.h"

#define NUM_PIN	21

static uint8_t	power_pin;
static uint8_t	dir_pin;
static uint8_t	input_pin;
static uint8_t	power[NUM_PIN];
static uint32_t	on_pins;

static void
port_init(void)
{
	/* Enable ADC */
	ADCSRA = ((1 << ADPS2) |
		  (1 << ADPS1) |
		  (1 << ADPS0) |
		  (1 << ADEN));

	/* Timer 0 */
	TCCR0A = ((1 << WGM01) |
		  (1 << WGM00));

	/* / 64 */
	TCCR0B = ((0 << CS02) |
		  (1 << CS01) |
		  (1 << CS00));

	/* Timer 1 */
	TCCR1B = ((0 << CS12) |
		  (1 << CS11) |
		  (1 << CS10));

	TCCR1A = ((0 << WGM12) |
		  (0 << WGM11) |
		  (1 << WGM10));

	OCR1AH = 0;
	OCR1BH = 0;

	/* Timer 2 */
	TCCR2B = ((1 << CS22) |
		  (0 << CS21) |
		  (0 << CS20));

	TCCR2A = ((1 << WGM20));

	memset(power, 0xff, NUM_PIN);
}

int
main (void)
{
	snek_uart_init();
	port_init();
	fprintf(stdout, "Welcome to Snek\n");
	snek_print_vals = true;
	for (;;)
		snek_parse();
}

static volatile uint8_t *
ddr_reg(uint8_t pin)
{
	if (pin < 8)
		return &DDRD;
	if (pin < 14)
		return &DDRB;
	return &DDRC;
}

static volatile uint8_t *
pin_reg(uint8_t pin)
{
	if (pin < 8)
		return &PIND;
	if (pin < 14)
		return &PINB;
	return &PINC;
}

static volatile uint8_t *
port_reg(uint8_t pin)
{
	if (pin < 8)
		return &PORTD;
	if (pin < 14)
		return &PORTB;
	return &PORTC;
}

static uint8_t
bit(uint8_t pin)
{
	if (pin < 8)
		;
	else if (pin < 14)
		pin = pin - 8;
	else
		pin = pin - 14;
	return 1 << pin;
}

static bool
has_pwm(uint8_t p)
{
	return ((p) == 3 || (p) == 5 || (p) == 6 || (p) == 9 || (p) == 10 || (p) == 11);
}

static volatile uint8_t * const PROGMEM ocr_reg_addrs[] = {
	[3] = &OCR2B,
	[5] = &OCR0B,
	[6] = &OCR0A,
	[9] = &OCR1AL,
	[10] = &OCR1BL,
	[11] = &OCR2A
};

static volatile uint8_t *
ocr_reg(uint8_t pin) {
	return (volatile uint8_t *) pgm_read_word(&ocr_reg_addrs[pin]);
}

static volatile uint8_t * const PROGMEM tcc_reg_addrs[] = {
	[3] = &TCCR2A,
	[5] = &TCCR0A,
	[6] = &TCCR0A,
	[9] = &TCCR1A,
	[10] = &TCCR1A,
	[11] = &TCCR2A,
};

static volatile uint8_t *
tcc_reg(uint8_t pin) {
	return (volatile uint8_t *) pgm_read_word(&tcc_reg_addrs[pin]);
}

static uint8_t const PROGMEM tcc_val_addrs[] = {
	[3] = 1 << COM2B1,
	[5] = 1 << COM0B1,
	[6] = 1 << COM0A1,
	[9] = 1 << COM1A1,
	[10] = 1 << COM1B1,
	[11] = 1 << COM2A1,
};

static uint8_t
tcc_val(uint8_t pin)
{
	return (uint8_t) pgm_read_byte(&tcc_val_addrs[pin]);
}

static void
set_dir(uint8_t pin, uint8_t d)
{
	volatile uint8_t *r = ddr_reg(pin);
	volatile uint8_t *p = port_reg(pin);
	uint8_t	b = bit(pin);

	if (d) {
		*r |= b;
		*p &= ~b;
	} else {
		*r &= ~b;
		*p |= b;
	}
}

static snek_poly_t
snek_error_duino_pin(snek_poly_t a)
{
	return snek_error("invalid pin %p", a);
}

snek_poly_t
snek_builtin_talkto(snek_poly_t a)
{
	snek_list_t *l;
	uint8_t p, d;

	switch (snek_poly_type(a)) {
	case snek_float:
		p = d = snek_poly_get_soffset(a);
		break;
	case snek_list:
		l = snek_poly_to_list(a);
		p = snek_poly_get_soffset(snek_list_get(l, 0, true));
		d = snek_poly_get_soffset(snek_list_get(l, 1, true));
		break;
	default:
		return snek_error_duino_pin(a);
	}
	if (!snek_abort) {
		if (p >= NUM_PIN)
			return snek_error_duino_pin(a);
		if (d >= NUM_PIN)
			return snek_error_duino_pin(a);
		set_dir(p, 1);
		set_dir(d, 1);
		power_pin = p;
		dir_pin = d;
	}
	return a;
}

snek_poly_t
snek_builtin_listento(snek_poly_t a)
{
	uint8_t p = snek_poly_get_soffset(a);
	if (p >= NUM_PIN)
		return snek_error_duino_pin(a);
	set_dir(p, 0);
	input_pin = p;
	return a;
}

static bool
is_on(uint8_t pin)
{
	return (on_pins >> pin) & 1;
}

static void
set_on(uint8_t pin)
{
	on_pins |= ((uint32_t) 1) << pin;
}

static void
set_off(uint8_t pin)
{
	on_pins &= ~((uint32_t) 1) << pin;
}

static snek_poly_t
set_out(uint8_t pin)
{
	uint8_t	p = 0;

	if (is_on(pin))
		p = power[pin];

	if (has_pwm(pin)) {
		if (0 < p && p < 255) {
			*ocr_reg(pin) = p;
			*tcc_reg(pin) |= tcc_val(pin);
			return SNEK_ZERO;
		}
		*tcc_reg(pin) &= ~tcc_val(pin);
	}
	if (p)
		*port_reg(pin) |= bit(pin);
	else
		*port_reg(pin) &= ~bit(pin);
	return SNEK_ZERO;
}

snek_poly_t
snek_builtin_setpower(snek_poly_t a)
{
	power[power_pin] = (uint8_t) (snek_poly_get_float(a) * 255.0f + 0.5f);
	return set_out(power_pin);
}

snek_poly_t
snek_builtin_setleft(void)
{
	set_on(dir_pin);
	return set_out(dir_pin);
}

snek_poly_t
snek_builtin_setright(void)
{
	set_off(dir_pin);
	return set_out(dir_pin);
}

snek_poly_t
snek_builtin_on(void)
{
	set_on(power_pin);
	return set_out(power_pin);
}

snek_poly_t
snek_builtin_off(void)
{
	set_off(power_pin);
	return set_out(power_pin);
}

snek_poly_t
snek_builtin_onfor(snek_poly_t a)
{
	snek_builtin_on();
	snek_builtin_time_sleep(a);
	snek_builtin_off();
	return a;
}

#define analog_reference 1

snek_poly_t
snek_builtin_read(void)
{
	if (input_pin >= 14) {
		uint8_t pin = input_pin - 14;
		ADMUX = (analog_reference << 6) | (pin & 7);
		ADCSRA |= (1 << ADSC);
		while (ADCSRA & (1 << ADSC))
			;
		uint8_t low = ADCL;
		uint8_t high = ADCH;
		float value = ((uint16_t) high << 8 | low) / 1023.0;

		return snek_float_to_poly(value);
	} else {
		return snek_bool_to_poly(*pin_reg(input_pin) & bit(input_pin));
	}
}

snek_poly_t
snek_builtin_stopall(void)
{
	uint8_t p;
	for (p = 0; p < NUM_PIN; p++)
		if (on_pins & ((uint32_t) 1 << p)) {
			set_off(p);
			set_out(p);
		}
	return SNEK_ZERO;
}

snek_poly_t
snek_builtin_time_sleep(snek_poly_t a)
{
	snek_soffset_t o = snek_poly_get_float(a) * 100.0f;
	while (o-- >= 0)
		_delay_ms(10);
	return SNEK_ONE;
}

