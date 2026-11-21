#include "board_io.h"

#include <errno.h>
#include <zephyr/drivers/gpio.h>

#define BUTTON_DEBOUNCE_MS 40

static const struct gpio_dt_spec leds[] = {
    [LED_RED] = GPIO_DT_SPEC_GET(DT_NODELABEL(red), gpios),
    [LED_GREEN] = GPIO_DT_SPEC_GET(DT_NODELABEL(green), gpios),
    [LED_BLUE] = GPIO_DT_SPEC_GET(DT_NODELABEL(blue), gpios),
};
static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
static int button_last;
static int button_stable;
static int64_t button_changed_ms;

/* Configure the common-anode LED outputs and read the initial B1 level. */
int board_io_init(void)
{
    for (size_t channel = 0; channel < ARRAY_SIZE(leds); channel++) {
        if (!gpio_is_ready_dt(&leds[channel])) {
            return -ENODEV;
        }
        int ret = gpio_pin_configure_dt(&leds[channel], GPIO_OUTPUT_INACTIVE);
        if (ret < 0) {
            return ret;
        }
    }

    if (!gpio_is_ready_dt(&button)) {
        return -ENODEV;
    }
    int ret = gpio_pin_configure_dt(&button, GPIO_INPUT);
    if (ret < 0) {
        return ret;
    }
    ret = gpio_pin_get_dt(&button);
    if (ret < 0) {
        return ret;
    }
    button_last = ret;
    button_stable = ret;
    return 0;
}

/* Select one LED channel. The active-low GPIO flags handle the inversion. */
int board_set_led(enum led_color color)
{
    for (size_t channel = 0; channel < ARRAY_SIZE(leds); channel++) {
        int ret = gpio_pin_set_dt(&leds[channel], channel == (size_t)color);
        if (ret < 0) {
            return ret;
        }
    }
    return 0;
}

/* Return true once per stable B1 press. The caller supplies uptime in ms. */
bool board_button_pressed(int64_t now_ms)
{
    int pressed = gpio_pin_get_dt(&button);
    if (pressed < 0) {
        return false;
    }
    if (pressed != button_last) {
        button_last = pressed;
        button_changed_ms = now_ms;
    }
    if (pressed == button_stable || now_ms - button_changed_ms < BUTTON_DEBOUNCE_MS) {
        return false;
    }
    button_stable = pressed;
    return pressed != 0;
}
