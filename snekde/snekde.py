#!/usr/bin/python3
#
# Copyright © 2019 Keith Packard <keithp@keithp.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#

import sys
import argparse
import time
import curses
import threading
import serial

from curses import ascii

stdscr = 0

snek_lock = threading.Lock()
snek_lock.acquire()

snek_current_window = 0
snek_edit_win = 0
snek_repl_win = 0

snek_monitor = False

snek_device = False

snek_dialog_waiting = False

#snek_debug_file = open('log', 'w')

#def snek_debug(message):
#    snek_debug_file.write(message + '\n')
#    snek_debug_file.flush()

#
# Read a character from the keyboard, releasing the
# global lock while blocked so that serial input can arrive
#

def my_getch(edit_win):
    global snek_lock, snek_current_window, snek_dialog_waiting
    while True:
        edit_win.set_cursor()
        snek_lock.release()
        c = edit_win.window.getch()
        snek_lock.acquire()
        if not snek_dialog_waiting:
            break
        if c == ord('\n'):
            snek_dialog_waiting.close()
            snek_dialog_waiting = False

    #
    # Check for resize
    #
    if c == curses.KEY_RESIZE:
        screen_resize()
    return c

class SnekDevice:
    """Link to snek device"""

    serial = False
    reader_thread = False
    writer_thread = False
    alive = False
    _reader_alive = False
    interface = False
    write_queue = False
    device = ""

    #
    # The interface needs to have a condition variable (cv) that is
    # signaled when data are available to write and function (receive)
    # that gets data that are read
    #

    def __init__(self, device, interface):
        self.interface = interface
        self.device = device
        self.serial = serial.Serial(port=device,
                                    baudrate=38400,
                                    bytesize=serial.EIGHTBITS,
                                    parity=serial.PARITY_NONE,
                                    stopbits=serial.STOPBITS_ONE,
                                    xonxoff=True,
                                    rtscts=False,
                                    dsrdtr=False)
        
    def start(self):
        """start worker threads"""

        self.alive = True
        self._reader_alive = True

        # start threads

        self.receiver_thread = threading.Thread(target=self.reader, name='rx')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()

        self.transmitter_thread = threading.Thread(target=self.writer, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()

    def stop_reader(self):
        if self.receiver_thread and threading.current_thread() != self.receiver_thread:
            self.serial.cancel_read()
            self.interface.cv.release()
            self.receiver_thread.join()
            self.interface.cv.acquire()

    def stop_writer(self):
        """set flag to stop worker threads"""
        if self.transmitter_thread and threading.current_thread() != self.transmitter_thread:
            self.interface.cv.notify()
            self.interface.cv.release()
            self.transmitter_thread.join()
            self.interface.cv.acquire()

    def close(self):
        self.alive = False
        self.stop_reader()
        self.stop_writer()
        try:
            self.serial.write_timeout = 1
            self.serial.write(b'\x0f')
        except serial.SerialException:
            pass
        self.serial.close()

    def reader(self):
        """loop and copy serial->"""
        try:
            while self.alive and self._reader_alive:
                # read all that is there or wait for one byte
                data = self.serial.read(self.serial.in_waiting or 1)
                if data:
                    self.interface.receive(str(data, encoding='utf-8', errors='ignore'))
        except serial.SerialException as e:
            self.interface.failed(self.device)
        finally:
            self.receiver_thread = False

    def writer(self):
        """Copy queued data to the serial port."""
        try:
            while self.alive:
                send_data = ""
                with self.interface.cv:
                    while not self.write_queue and self.alive:
                        self.interface.cv.wait()
                    if not self.alive:
                        return
                    send_data = self.write_queue
                    self.write_queue = False
                self.serial.write(send_data.encode('utf-8'))
        except serial.SerialException as e:
            self.interface.failed(self.device)
        finally:
            self.transmitter_thread = False

    def write(self, data):
        if self.write_queue:
            self.write_queue += data
        else:
            self.write_queue = data
        self.interface.cv.notify()

    def command(self, data):
        self.write("\x0e" + data)

class EditWin:
    """Editable text object"""

    window = 0
    lines = 0
    y = 0
    point = 0
    top_line = 0
    tab_width = 4
    text = ""
    cut = ""
    mark = -1

    undo = []

    def __init__(self, lines, cols, y, x):
        self.lines = lines
        self.y = y
        self.window = curses.newwin(lines, cols, y, x)
        self.window.keypad(True)

    # Set contents, resetting state back to start
    
    def set_text(self, text):
        self.text = text
        self.point = 0
        self.mark = -1
        self.top_line = 0

    # Convert text index to x/y coord
    
    def point_to_cursor(self, point):
        line = -1
        col = 0
        for s in self.text[:point].split('\n'):
            line += 1
            col = len(s)
        return(col, line)

    # Convert x/y coord to text index
    
    def cursor_to_point(self, cursor):
        (cur_col, cur_line) = cursor
        if cur_line < 0:
            cur_line = 0
        elif cur_line >= len(self.text.split('\n')):
            cur_line = len(self.text.split('\n')) - 1
        bol = 0
        line = 0
        point = 0
        for s in self.text.split('\n'):
            point = bol + min(cursor[0], len(s))
            if line == cur_line:
                break
            line += 1
            bol += len(s) + 1
        return point

    # Make sure current point is visible in the window

    def scroll_to_point(self):
        while self.point_to_cursor(self.point)[1] < self.top_line:
            self.top_line -= 1
        while self.point_to_cursor(self.point)[1] >= self.top_line + self.lines:
            self.top_line += 1

    # Repaint the window

    def repaint(self):
        self.window.erase()
        self.scroll_to_point()
        selection = self.get_selection()
        if selection:
            start_selection = self.point_to_cursor(selection[0])
            end_selection = self.point_to_cursor(selection[1])
        line = 0
        for s in self.text.split('\n'):
            if self.top_line <= line and line < self.top_line + self.lines:

                # Paint the marked region in reverse video

                if selection and start_selection[1] <= line and line <= end_selection[1]:
                    if line == start_selection[1]:
                        before = s[:start_selection[0]]
                        middle = s[start_selection[0]:]
                    else:
                        before = ""
                        middle = s
                    if line == end_selection[1]:
                        after = middle[end_selection[0]:]
                        middle = middle[:end_selection[0]]
                    else:
                        after = ""
                    self.window.addstr(line - self.top_line, 0, before)
                    self.window.addstr(line - self.top_line, len(before), middle, curses.A_REVERSE)
                    self.window.addstr(line - self.top_line, len(before) + len(middle), after)
                else:
                    self.window.addstr(line - self.top_line, 0, s)
            line += 1
        self.window.refresh()

    # Set window size

    def resize(self, lines, cols, y, x):
        self.lines = lines
        self.window.resize(lines, cols)
        self.window.mvwin(y, x)
        self.repaint()

    # This window is the input window, set the cursor position
    # to the current point

    def set_cursor(self):
        p = self.point_to_cursor(self.point)
        self.window.move(p[1] - self.top_line, p[0])
        self.window.refresh()

    # Find the indent of the specified line

    def indent_at(self, line):
        bol = self.cursor_to_point((0, line))
        point = bol
        while point < len(self.text) and self.text[point] == " ":
            point += 1
        return point - bol

    # Return the last character on the specified line

    def last_ch(self, line):
        eol = self.cursor_to_point((65536, line))
        point = eol - 1
        while point > 0 and self.text[point] == " ":
            point -= 1
        return self.text[point]

    # Is 'point' in the indentation of its line?

    def in_indent(self, point):
        cursor = self.point_to_cursor(point)
        if cursor[0] > 0 and self.indent_at(cursor[1]) >= cursor[0]:
            return True
        return False

    # Move left

    def left(self):
        if self.point > 0:
            self.point -= 1

    # Move up

    def up(self):
        pos = self.point_to_cursor(self.point)
        point = self.cursor_to_point((pos[0], pos[1]-1))
        if point == self.point:
            point = self.cursor_to_point((0, pos[1]))
        self.point = point

    # Move right

    def right(self):
        if self.point < len(self.text):
            self.point += 1

    # Move down

    def down(self):
        pos = self.point_to_cursor(self.point)
        point = self.cursor_to_point((pos[0], pos[1]+1))
        if point == self.point:
            point = self.cursor_to_point((65536, pos[1]))
        self.point = point

    # Move to begining of line

    def bol(self):
        pos = self.point_to_cursor(self.point)
        self.point = self.cursor_to_point((0, pos[1]))

    # Move to end of line

    def eol(self):
        pos = self.point_to_cursor(self.point)
        self.point = self.cursor_to_point((65536, pos[1]))

    def push_undo(self, point, operation):
        self.undo.append((point, operation, self.point, self.mark))

    def pop_undo(self):
        if not self.undo:
            return False
        (point, operation, self_point, self_mark) = self.undo.pop()

        if isinstance(operation, str):
            # Replace deleted text
            self.text = self.text[:point] + operation + self.text[point:]
        else:
            # Delete inserted text
            self.text = self.text[:point] + self.text[point+operation:]

        self.point = self_point
        self.mark = self_mark
        return True

    # Insert some text, adjusting self.point and self.mark if the text
    # is before them

    def insert(self, point, text):
        self.push_undo(point, len(text))
        self.text = self.text[:point] + text + self.text[point:]
        if point < self.point:
            self.point += len(text)
        if point < self.mark:
            self.mark += len(text)

    def insert_at_point(self, text):
        self.insert(self.point, text)
        self.point += len(text)

    # Delete some text, adjusting self.point if the delete starts
    # before it

    def _adjust_delete_position(self, delete_point, delete_count, moving_point, is_mark):
        if delete_point <= moving_point and moving_point < delete_point + delete_count:
            if is_mark:
                return -1
            moving_point = delete_point
        elif delete_point + delete_count <= moving_point:
            moving_point -= delete_count
        return moving_point

    def delete(self, point, count):
        self.push_undo(point, self.text[point:point+count])
        self.text = self.text[:point] + self.text[point + count:]
        self.point = self._adjust_delete_position(point, count, self.point, False)
        if self.mark >= 0:
            self.mark = self._adjust_delete_position(point, count, self.mark, True)

    def delete_at_point(self, count):
        self.delete(self.point, count)

    # Delete back to the previous tab stop

    def backtab(self):
        pos = self.point_to_cursor(self.point)
        if pos[0] == 0:
            return
        to_remove = pos[0] % self.tab_width
        if to_remove == 0:
            to_remove = self.tab_width
        self.point -= to_remove
        self.delete_at_point(to_remove)

    # Delete something. If there's a mark, delete that.  otherwise,
    # delete backwards, if in indent of the line, backtab
    
    def backspace(self):
        selection = self.get_selection()
        if selection:
            self.delete(selection[0], selection[1] - selection[0])
            self.mark = -1
        elif self.point > 0:
            if self.in_indent(self.point):
                self.backtab()
            else:
                self.left()
                self.delete_at_point(1)

    # Set or clear the 'mark', which defines
    # the other end of the current selection

    def toggle_mark(self):
        if self.mark >= 0:
            self.mark = -1
        else:
            self.mark = self.point

    # Return the extent of the current selection, False if none

    def get_selection(self):
        if self.mark >= 0:
            return (min(self.mark, self.point), max(self.mark, self.point))
        else:
            return False

    # Copy from mark to point and place in cut buffer
    # delete from text if requested

    def copy(self, delete=False):
        selection = self.get_selection()
        if selection:
            (start, end) = selection
                
            self.cut = self.text[start:end]
            if delete:
                self.delete(start, end - start)
            self.mark = -1

    # Paste any cut buffer at point
    
    def paste(self):
        if self.cut:
            self.insert_at_point(self.cut)

    # Set indent of current line to 'want'. Leave point
    # at the end of the indent

    def indent(self, want):
        self.bol()
        have = 0
        while self.point < len(self.text) and self.text[self.point] == " ":
            self.right()
            have += 1
        if have < want:
            self.insert_at_point(" " * (want - have))
        elif have > want:
            self.delete_at_point(have - want)

    # Automatically indent the current line,
    # using the previous line as a guide

    def auto_indent(self):
        cursor = self.point_to_cursor(self.point)
        want = 0
        if cursor[1] > 0:
            want = self.indent_at(cursor[1] - 1)
            if self.last_ch(cursor[1] - 1) == ":":
                want += self.tab_width
        self.indent(want)

    # Delete to end of line, or delete newline if at end of line

    def delete_to_eol(self):
        current = self.point_to_cursor(self.point)
        eol = self.cursor_to_point((65536, current[1]))
        if self.point == eol:
            self.delete_at_point(1)
        else:
            self.delete_at_point(eol - self.point)

    # Read a character for this window

    def getch(self):
        self.repaint()
        return my_getch(self)

    # Return the contents of the previous line

    def prev_line(self):
        pos = self.point_to_cursor(self.point)
        if pos[1] == 0:
            return ""
        start = self.cursor_to_point((0, pos[1]-1))
        end = self.cursor_to_point((0, pos[1]))
        return self.text[start:end]

    def dispatch(self, ch):
        if ch == 0:
            self.toggle_mark()
        elif ch == ord('c') & 0x1f:
            self.copy(delete=False)
        elif ch == ord('x') & 0x1f:
            self.copy(delete=True)
        elif ch == ord('v') & 0x1f:
            self.paste()
        elif ch == ord('k') & 0x1f:
            self.delete_to_eol()
        elif ch == ord('z') & 0x1f:
            self.pop_undo()
        if ch == curses.KEY_LEFT or ch == ord('b') & 0x1f:
            self.left()
        elif ch == curses.KEY_RIGHT or ch == ord('f') & 0x1f:
            self.right()
        elif ch == curses.KEY_UP or ch == ord('p') & 0x1f:
            self.up()
        elif ch == curses.KEY_DOWN or ch == ord('n') & 0x1f:
            self.down()
        elif ch == curses.KEY_HOME or ch == ord('a') & 0x1f:
            self.bol()
        elif ch == curses.KEY_END or ch == ord('e') & 0x1f:
            self.eol()
        elif ch == ord('\t'):
            self.auto_indent()
        elif ch in (curses.ascii.BS, curses.KEY_BACKSPACE, curses.ascii.DEL):
            self.backspace()
        elif curses.ascii.isprint(ch) or ch == ord('\n'):
            self.insert_at_point(chr(ch))

class ErrorWin:
    """Show an error message"""
    label = ""
    x = 0
    y = 0
    nlines = 5
    ncols = 40
    inputthread = True

    window = False

    def __init__(self, label, inputthread=True):
        self.label = label
        self.inputthread = inputthread
        self.ncols = min(curses.COLS, max(40, len(label) + 2))
        self.x = (curses.COLS - self.ncols) // 2
        self.y = (curses.LINES - self.nlines) // 2
        self.window = curses.newwin(self.nlines, self.ncols, self.y, self.x)
        self.window.keypad(True)
        self.run_dialog()

    def repaint(self):
        self.window.border()
        l = len(self.label)
        if l > self.ncols:
            l = self.ncols
        self.window.addstr(1, (self.ncols - l) // 2, self.label)
        self.window.addstr(3, 2, "OK")

    def close(self):
        del self.window
        screen_repaint()

    def run_dialog(self):
        global snek_dialog_waiting
        self.repaint()
        self.window.move(3, 4)
        self.window.refresh()
        if self.inputthread:
            self.window.getstr()
            self.close()
        else:
            snek_dialog_waiting = self

class GetTextWin:
    """Prompt for line of text"""

    label = ""
    prompt = ""
    x = 0
    y = 0
    nlines = 5
    ncols = 40

    window = False

    def __init__(self, label, prompt="File:"):
        self.label = label
        self.prompt = prompt
        self.x = (curses.COLS - self.ncols) // 2
        self.y = (curses.LINES - self.nlines) // 2
        self.window = curses.newwin(self.nlines, self.ncols, self.y, self.x)
        self.window.keypad(True)

    def repaint(self):
        self.window.border()
        self.window.addstr(1, (self.ncols - len(self.label)) // 2, self.label)
        self.window.addstr(3, 2, self.prompt)

    def run_dialog(self):
        self.repaint()
        self.window.move(3, 8)
        curses.echo()
        name = self.window.getstr()
        curses.noecho()
        del self.window
        screen_repaint()
        return str(name, encoding='utf-8', errors='ignore')

def screen_get_sizes():
    repl_lines = curses.LINES // 3
    edit_lines = curses.LINES - repl_lines - 2
    edit_y = 1
    repl_y = edit_y + edit_lines + 1
    return (edit_lines, edit_y, repl_lines, repl_y)

help_text = (
    ("F1", "Device"),
    ("F2", "Get"),
    ("F3", "Put"),
    ("F4", "Quit"),
    ("F5", "Load"),
    ("F6", "Save")
    )

def screen_paint():
    global stdscr, snek_device, snek_edit_win
    help_col = 0
    help_cols = min(curses.COLS // len(help_text), 13)
    stdscr.addstr(0, 0, " " * curses.COLS)
    for (f, t) in help_text:
        stdscr.addstr(0, help_col, " %2s: %-6s " % (f, t), curses.A_REVERSE)
        help_col += help_cols
    device_name = "<no device>"
    if snek_device:
        device_name = snek_device.device
    device_col = curses.COLS - len(device_name)
    if device_col < 0:
        device_col = 0
    mid_y = snek_edit_win.y + snek_edit_win.lines
    stdscr.addstr(mid_y, device_col, device_name, curses.A_REVERSE)
    if device_col >= 6:
        stdscr.addstr(mid_y, device_col - 6, "      ", curses.A_REVERSE)
    for col in range(0,device_col - 6,5):
        stdscr.addstr(mid_y, col, "snek ", curses.A_REVERSE)
    stdscr.refresh()
    
# Repaint everything, as when a dialog goes away

def screen_repaint():
    global snek_edit_win, snek_repl_win
    snek_edit_win.repaint()
    snek_repl_win.repaint()
    screen_paint()
    if snek_current_window:
        snek_current_window.set_cursor()

def screen_resize():
    global snek_edit_win, snek_repl_win
    curses.update_lines_cols()
    (edit_lines, edit_y, repl_lines, repl_y) = screen_get_sizes()
    screen_paint()
    snek_edit_win.resize(edit_lines, curses.COLS, edit_y, 0)
    snek_repl_win.resize(repl_lines, curses.COLS, repl_y, 0)

def screen_init(text):
    global stdscr, snek_edit_win, snek_repl_win
    stdscr = curses.initscr()
    curses.noecho()
    curses.raw()
    stdscr.keypad(True)
    (edit_lines, edit_y, repl_lines, repl_y) = screen_get_sizes()
    snek_edit_win = EditWin(edit_lines, curses.COLS, edit_y, 0)
    if text:
        snek_edit_win.set_text(text)
    snek_repl_win = EditWin(repl_lines, curses.COLS, repl_y, 0)
    screen_paint()

def screen_fini():
    global stdscr
    stdscr.keypad(False)
    curses.noraw()
    curses.echo()
    curses.endwin()

def snekde_open_device():
    global snek_device, snek_monitor
    dialog = GetTextWin("Open Device", prompt="Port:")
    name = dialog.run_dialog()
    try:
        device = SnekDevice(name, snek_monitor)
        device.start()
        if snek_device:
            snek_device.close()
            del snek_device
        snek_device = device
        screen_paint()
    except OSError as e:
        message = e.strerror
        if not message:
            message = "failed"
        ErrorWin("%s: %s" % (name, message))

def snekde_get_text():
    global snek_edit_win, snek_device
    snek_edit_win.set_text("")
    snek_device.command("eeprom.show(1)\n")

def snekde_put_text():
    global snek_edit_win, snek_device
    snek_device.command("eeprom.write()\n")
    snek_device.write(snek_edit_win.text + '\x04')
    snek_device.command("eeprom.load()\n")
    snek_device.command('print("All done")\n')

def snekde_load_file():
    global snek_edit_win
    dialog = GetTextWin("Load File", prompt="File:")
    name = dialog.run_dialog()
    try:
        with open(name, 'r') as myfile:
            data = myfile.read()
            snek_edit_win.set_text(data)
    except OSError as e:
        ErrorWin("%s: %s" % (e.filename, e.strerror))
        

def snekde_save_file():
    global snek_edit_win
    dialog = GetTextWin("Save File", prompt="File:")
    name = dialog.run_dialog()
    try:
        with open(name, 'w') as myfile:
            myfile.write(snek_edit_win.text)
    except OSError as e:
        ErrorWin("%s: %s" % (e.filename, e.strerror))

def run():
    global snek_current_window, snek_edit_win, snek_repl_win, snek_device
    snek_current_window = snek_edit_win
    while True:
        ch = snek_current_window.getch()
        if ch == curses.KEY_NPAGE or ch == curses.KEY_PPAGE:
            if snek_current_window is snek_edit_win:
                snek_current_window = snek_repl_win
            else:
                snek_current_window = snek_edit_win
            continue
        if ch == 3:
            if snek_device:
                snek_device.write(chr(3))
        elif ch == curses.KEY_F1:
            snekde_open_device()
        elif ch == curses.KEY_F2:
            if snek_device:
                snekde_get_text()
            else:
                ErrorWin("No device")
        elif ch == curses.KEY_F3:
            if snek_device:
                snekde_put_text()
            else:
                ErrorWin("No device")
        elif ch == curses.KEY_F4:
            sys.exit(0)
        elif ch == curses.KEY_F5:
            snekde_load_file()
        elif ch == curses.KEY_F6:
            snekde_save_file()
        else:
            snek_current_window.dispatch(ch)
            if ch == ord('\n'):
                if snek_current_window is snek_edit_win:
                    snek_current_window.auto_indent()
                elif snek_device:
                    data = snek_repl_win.prev_line()
                    while True:
                        if data[:2] == "> " or data[:2] == "+ ":
                            data = data[2:]
                        elif data[:1] == ">" or data[:1] == "+":
                            data = data[1:]
                        else:
                            break
                    snek_device.command(data)


# Class to monitor the serial device for data and
# place in approprite buffer. Will be used as
# parameter to SnekDevice, and so it must expose
# 'cv' as a condition variable and 'receive' as a
# function to get data

class SnekMonitor:

    def __init__(self):
        global snek_lock
        self.cv = threading.Condition(snek_lock)

    # Reading text to snek_edit_win instead of snek_repl_win

    getting_text = False

    def add_to(self, window, data):
        global snek_current_window, snek_repl_win
        follow = window == snek_repl_win and window.point == len(window.text)
        window.text += data
        if follow:
            window.point += len(data)
        window.repaint()
        if snek_current_window:
            snek_current_window.set_cursor()

    def receive(self, data):
        global snek_edit_win, snek_repl_win, snek_lock
        data_edit = ""
        data_repl = ""
        for c in data:
            if c == '\x02':
                self.getting_text = True
            elif c == '\x03':
                self.getting_text = False
            elif c == '\x00':
                continue
            elif c == '\r':
                continue
            else:
                if self.getting_text:
                    data_edit += c
                else:
                    data_repl += c
        with snek_lock:
            if data_edit:
                self.add_to(snek_edit_win, data_edit)
            if data_repl:
                self.add_to(snek_repl_win, data_repl)

    def failed(self, device):
        global snek_device, snek_lock
        with snek_lock:
            if snek_device:
                snek_device.close()
                del snek_device
                snek_device = False
            ErrorWin("Device %s failed" % device, inputthread=False)

def main():
    global snek_device, snek_edit_win, snek_monitor

    snek_monitor = SnekMonitor()

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--list", action='store_true', help="List available serial devices")
    arg_parser.add_argument("--port", help="Serial device")
    arg_parser.add_argument("file", nargs="*", help="Read file into edit window")
    args = arg_parser.parse_args()
    snek_device = False
    if args.port:
        try:
            snek_device = SnekDevice(args.port, snek_monitor)
        except OSError as e:
            print(e.strerror, file=sys.stderr)
            exit(1)
    text = ""
    if args.file:
        try:
            with open(args.file[0], 'r') as myfile:
                text = myfile.read()
        except OSError as e:
            print("%s: %s", (e.filename, e.strerror), file=sys.stderr)
            exit(1)
    try:
        screen_init(text)
        if snek_device:
            snek_device.start()
        run()
    finally:
        screen_fini()

main()
