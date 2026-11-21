#ifndef DS_DRONE_BOARD_IO_H
#define DS_DRONE_BOARD_IO_H

#include <stdbool.h>
#include <stdint.h>

enum led_color {
    LED_RED,
    LED_GREEN,
    LED_BLUE,
};

int board_io_init(void);
int board_set_led(enum led_color color);
bool board_button_pressed(int64_t now_ms);

#endif
