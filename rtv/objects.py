# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import re
import os
import time
import signal
import inspect
import weakref
import logging
import threading
import curses
import curses.ascii
from contextlib import contextmanager

import six
import praw
import requests

from . import exceptions


_logger = logging.getLogger(__name__)


@contextmanager
def curses_session():
    """
    Setup terminal and initialize curses. Most of this copied from
    curses.wrapper in order to convert the wrapper into a context manager.
    """

    try:
        # Curses must wait for some time after the Escape key is pressed to
        # check if it is the beginning of an escape sequence indicating a
        # special key. The default wait time is 1 second, which means that
        # http://stackoverflow.com/questions/27372068
        os.environ['ESCDELAY'] = '25'

        # Initialize curses
        stdscr = curses.initscr()

        # Turn off echoing of keys, and enter cbreak mode, where no buffering
        # is performed on keyboard input
        curses.noecho()
        curses.cbreak()

        # In keypad mode, escape sequences for special keys (like the cursor
        # keys) will be interpreted and a special value like curses.KEY_LEFT
        # will be returned
        stdscr.keypad(1)

        # Start color, too.  Harmless if the terminal doesn't have color; user
        # can test with has_color() later on.  The try/catch works around a
        # minor bit of over-conscientiousness in the curses module -- the error
        # return from C start_color() is ignorable.
        try:
            curses.start_color()
        except:
            pass

        # Hide the blinking cursor
        curses.curs_set(0)

        # Assign the terminal's default (background) color to code -1
        curses.use_default_colors()

        yield stdscr

    finally:
        if 'stdscr' in locals():
            stdscr.keypad(0)
            curses.echo()
            curses.nocbreak()
            curses.endwin()


class LoadScreen(object):
    """
    Display a loading dialog while waiting for a blocking action to complete.

    This class spins off a separate thread to animate the loading screen in the
    background. The loading thread also takes control of stdscr.getch(). If
    an exception occurs in the main thread while the loader is active, the
    exception will be caught, attached to the loader object, and displayed as
    a notification. The attached exception can be used to trigger context
    sensitive actions. For example, if the connection hangs while opening a
    submission, the user may press ctrl-c to raise a KeyboardInterrupt. In this
    case we would *not* want to refresh the current page.

    >>> with self.terminal.loader(...) as loader:
    >>>     # Perform a blocking request to load content
    >>>     blocking_request(...)
    >>>
    >>> if loader.exception is None:
    >>>     # Only run this if the load was successful
    >>>     self.refresh_content()

    When a loader is nested inside of itself, the outermost loader takes
    priority and all of the nested loaders become no-ops. Call arguments given
    to nested loaders will be ignored, and errors will propagate to the parent.

    >>> with self.terminal.loader(...) as loader:
    >>>
    >>>     # Additional loaders will be ignored
    >>>     with self.terminal.loader(...):
    >>>         raise KeyboardInterrupt()
    >>>
    >>>     # This code will not be executed because the inner loader doesn't
    >>>     # catch the exception
    >>>     assert False
    >>>
    >>> # The exception is finally caught by the outer loader
    >>> assert isinstance(terminal.loader.exception, KeyboardInterrupt)
    """

    EXCEPTION_MESSAGES = [
        (exceptions.RTVError, '{0}'),
        (praw.errors.OAuthException, 'OAuth Error'),
        (praw.errors.OAuthScopeRequired, 'Not logged in'),
        (praw.errors.LoginRequired, 'Not logged in'),
        (praw.errors.InvalidCaptcha, 'Error, captcha required'),
        (praw.errors.InvalidSubreddit, '{0.args[0]}'),
        (praw.errors.PRAWException, '{0.__class__.__name__}'),
        (requests.exceptions.RequestException, '{0.__class__.__name__}'),
    ]

    def __init__(self, terminal):

        self.exception = None
        self.catch_exception = None
        self.depth = 0
        self._terminal = weakref.proxy(terminal)
        self._args = None
        self._animator = None
        self._is_running = None

    def __call__(
            self,
            message='Downloading',
            trail='...',
            delay=0.5,
            interval=0.4,
            catch_exception=True):
        """
        Params:
            delay (float): Length of time that the loader will wait before
                printing on the screen. Used to prevent flicker on pages that
                load very fast.
            interval (float): Length of time between each animation frame.
            message (str): Message to display
            trail (str): Trail of characters that will be animated by the
                loading screen.
            catch_exception (bool): If an exception occurs while the loader is
                active, this flag determines whether it is caught or allowed to
                bubble up.
        """

        if self.depth > 0:
            return self

        self.exception = None
        self.catch_exception = catch_exception
        self._args = (delay, interval, message, trail)
        return self

    def __enter__(self):

        self.depth += 1
        if self.depth > 1:
            return self

        self._animator = threading.Thread(target=self.animate, args=self._args)
        self._animator.daemon = True
        self._is_running = True
        self._animator.start()
        return self

    def __exit__(self, exc_type, e, exc_tb):

        self.depth -= 1
        if self.depth > 0:
            return

        self._is_running = False
        self._animator.join()

        if e is None or not self.catch_exception:
            # Skip exception handling
            return

        self.exception = e
        exc_name = type(e).__name__
        _logger.info('Loader caught: %s - %s', exc_name, e)

        if isinstance(e, KeyboardInterrupt):
            # Don't need to print anything for this one, just swallow it
            return True

        for e_type, message in self.EXCEPTION_MESSAGES:
            # Some exceptions we want to swallow and display a notification
            if isinstance(e, e_type):
                self._terminal.show_notification(message.format(e))
                return True

    def animate(self, delay, interval, message, trail):

        # The animation starts with a configurable delay before drawing on the
        # screen. This is to prevent very short loading sections from
        # flickering on the screen before immediately disappearing.
        with self._terminal.no_delay():
            start = time.time()
            while (time.time() - start) < delay:
                # Pressing escape triggers a keyboard interrupt
                if self._terminal.getch() == self._terminal.ESCAPE:
                    os.kill(os.getpid(), signal.SIGINT)
                    self._is_running = False

                if not self._is_running:
                    return
                time.sleep(0.01)

        # Build the notification window
        message_len = len(message) + len(trail)
        n_rows, n_cols = self._terminal.stdscr.getmaxyx()
        s_row = (n_rows - 3) // 2
        s_col = (n_cols - message_len - 1) // 2
        window = curses.newwin(3, message_len + 2, s_row, s_col)

        # Animate the loading prompt until the stopping condition is triggered
        # when the context manager exits.
        with self._terminal.no_delay():
            while True:
                for i in range(len(trail) + 1):
                    if not self._is_running:
                        window.erase()
                        del window
                        self._terminal.stdscr.touchwin()
                        self._terminal.stdscr.refresh()
                        return

                    window.erase()
                    window.border()
                    self._terminal.add_line(window, message + trail[:i], 1, 1)
                    window.refresh()

                    # Break up the designated sleep interval into smaller
                    # chunks so we can more responsively check for interrupts.
                    for _ in range(int(interval/0.01)):
                        # Pressing escape triggers a keyboard interrupt
                        if self._terminal.getch() == self._terminal.ESCAPE:
                            os.kill(os.getpid(), signal.SIGINT)
                            self._is_running = False
                            break
                        time.sleep(0.01)


class Color(object):
    """
    Color attributes for curses.
    """

    RED = curses.A_NORMAL
    GREEN = curses.A_NORMAL
    YELLOW = curses.A_NORMAL
    BLUE = curses.A_NORMAL
    MAGENTA = curses.A_NORMAL
    CYAN = curses.A_NORMAL
    WHITE = curses.A_NORMAL

    _colors = {
        'RED': (curses.COLOR_RED, -1),
        'GREEN': (curses.COLOR_GREEN, -1),
        'YELLOW': (curses.COLOR_YELLOW, -1),
        'BLUE': (curses.COLOR_BLUE, -1),
        'MAGENTA': (curses.COLOR_MAGENTA, -1),
        'CYAN': (curses.COLOR_CYAN, -1),
        'WHITE': (curses.COLOR_WHITE, -1),
    }

    @classmethod
    def init(cls):
        """
        Initialize color pairs inside of curses using the default background.

        This should be called once during the curses initial setup. Afterwards,
        curses color pairs can be accessed directly through class attributes.
        """

        for index, (attr, code) in enumerate(cls._colors.items(), start=1):
            curses.init_pair(index, code[0], code[1])
            setattr(cls, attr, curses.color_pair(index))

    @classmethod
    def get_level(cls, level):

        levels = [cls.MAGENTA, cls.CYAN, cls.GREEN, cls.YELLOW]
        return levels[level % len(levels)]


class Controller(object):
    """
    Event handler for triggering functions with curses keypresses.

    Register a keystroke to a class method using the @register decorator.
    >>> @Controller.register('a', 'A')
    >>> def func(self, *args)
    >>>     ...

    Register a KeyBinding that can be defined later by the config file
    >>> @Controller.register(Command("UPVOTE"))
    >>> def upvote(self, *args)
    >>      ...

    Bind the controller to a class instance and trigger a key. Additional
    arguments will be passed to the function.
    >>> controller = Controller(self)
    >>> controller.trigger('a', *args)
    """

    character_map = {}

    def __init__(self, instance, keymap=None):

        self.instance = instance
        # Build a list of parent controllers that follow the object's MRO
        # to check if any parent controllers have registered the keypress
        self.parents = inspect.getmro(type(self))[:-1]
        # Keep track of last key press for doubles like `gg`
        self.last_char = None

        if not keymap:
            return

        # Go through the controller and all of it's parents and look for
        # Command objects in the character map. Use the keymap the lookup the
        # keys associated with those command objects and add them to the
        # character map.
        for controller in self.parents:
            for command, func in controller.character_map.copy().items():
                if isinstance(command, Command):
                    for key in keymap.get(command):
                        val = keymap.parse(key)
                        # If a double key press is defined, the first half
                        # must be unbound
                        if isinstance(val, tuple):
                            if controller.character_map.get(val[0]) is not None:
                                raise exceptions.ConfigError(
                                    "Invalid configuration! `%s` is bound to "
                                    "duplicate commands in the "
                                    "%s" % (key, controller.__name__))
                            # Mark the first half of the double with None so
                            # that no other command can use it
                            controller.character_map[val[0]] = None

                        # Check if the key is already programmed to trigger a
                        # different function.
                        if controller.character_map.get(val, func) != func:
                            raise exceptions.ConfigError(
                                "Invalid configuration! `%s` is bound to "
                                "duplicate commands in the "
                                "%s" % (key, controller.__name__))
                        controller.character_map[val] = func

    def trigger(self, char, *args, **kwargs):

        if isinstance(char, six.string_types) and len(char) == 1:
            char = ord(char)

        func = None
        # Check if the controller (or any of the controller's parents) have
        # registered a function to the given key
        for controller in self.parents:
            func = controller.character_map.get((self.last_char, char))
            if func:
                break
            func = controller.character_map.get(char)
            if func:
                break

        if func:
            self.last_char = None
            return func(self.instance, *args, **kwargs)
        else:
            self.last_char = char
            return None

    @classmethod
    def register(cls, *chars):
        def inner(f):
            for char in chars:
                if isinstance(char, six.string_types) and len(char) == 1:
                    cls.character_map[ord(char)] = f
                else:
                    cls.character_map[char] = f
            return f
        return inner


class Command(object):
    """
    Minimal class that should be used to wrap abstract commands that may be
    implemented as one or more physical keystrokes.

    E.g. Command("REFRESH") can be represented by the KeyMap to be triggered
         by either `r` or `F5`
    """

    def __init__(self, val):
        self.val = val.upper()

    def __repr__(self):
        return 'Command(%s)' % self.val

    def __eq__(self, other):
        return repr(self) == repr(other)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(repr(self))


class KeyMap(object):
    """
    Mapping between commands and the keys that they represent.
    """

    def __init__(self, bindings):
        self._keymap = None
        self.set_bindings(bindings)

    def set_bindings(self, bindings):
        # Clear the keymap before applying the bindings to avoid confusion.
        # If a user defines custom bindings in their config file, they must
        # explicitly define ALL of the bindings.
        self._keymap = {}
        for command, keys in bindings.items():
            if not isinstance(command, Command):
                command = Command(command)
            self._keymap[command] = keys

    def get(self, command):
        if not isinstance(command, Command):
            command = Command(command)
        try:
            return self._keymap[command]
        except KeyError:
            raise exceptions.ConfigError('Invalid configuration! `%s` key is '
                                         'undefined' % command.val)

    @classmethod
    def parse(cls, key):
        """
        Parse a key represented by a string and return its character code.
        """

        try:
            if isinstance(key, int):
                return key
            elif re.match('[<]KEY_.*[>]', key):
                # Curses control character
                return getattr(curses, key[1:-1])
            elif re.match('[<].*[>]', key):
                # Ascii control character
                return getattr(curses.ascii, key[1:-1])
            elif key.startswith('0x'):
                # Ascii hex code
                return int(key, 16)
            elif len(key) == 2:
                # Double presses
                return tuple(cls.parse(k) for k in key)
            else:
                # Ascii character
                code = ord(key)
                if 0 <= code <= 255:
                    return code
                # Python 3.3 has a curses.get_wch() function that we can use
                # for unicode keys, but Python 2.7 is limited to ascii.
                raise exceptions.ConfigError('Invalid configuration! `%s` is '
                                             'not in the ascii range' % key)

        except (AttributeError, ValueError, TypeError):
            raise exceptions.ConfigError('Invalid configuration! "%s" is not a '
                                         'valid key' % key)


class Navigator(object):
    """
    The navigator holds the state of the main content window. This includes all
    of the information necessary to draw the screen, including the current
    position of the cursor and any offsets applied to the top of the page.
    """

    def __init__(self, content, page_index=0, cursor_index=0, top_offset=0):

        self.page_index = page_index
        self.cursor_index = cursor_index
        self.top_offset = top_offset

        self._n_rows = None
        self._n_cols = None
        self._n_subwindows = None
        self._content = content

    @property
    def absolute_index(self):
        """
        Return the index of the currently selected item
        """
        return self.page_index + self.cursor_index

    def set_window_params(self, n_rows, n_cols, n_subwindows):
        """
        Save the size of the content window, this will be used to make movement
        calculations in the next loop. This method should be called every time
        the content window is re-drawn.

        Params:
            n_rows (int): Height of the content window
            n_cols (int): Width of the content window
            n_subwindows (int): The number of submissions currently drawn on
                the content window.
        """
        self._n_rows = n_rows
        self._n_cols = n_cols
        self._n_subwindows = n_subwindows

    def move_cursor_up(self):
        """
        Attempt to move the cursor up the screen by one item.
        """
        self._content.get(self.absolute_index - 1)
        if self.cursor_index > 1:
            self.cursor_index -= 1
        else:
            self._align_top(self.absolute_index - 1)

    def move_cursor_down(self):
        """
        Attempt to move the cursor down the screen by one item.
        """
        self._content.get(self.absolute_index + 1)
        if self.page_index < 0:
            self.page_index = 0
        elif self.cursor_index < self._n_subwindows - 2:
            self.cursor_index += 1
        else:
            self._align_bottom(self.absolute_index + 1)

    def move_page_up(self):
        """
        Move the page up by making the top item on the screen the bottom item.
        """
        self._content.get(self.page_index - 1)
        if self._n_subwindows > 1:
            self._align_bottom(self.page_index)
        else:
            self._align_bottom(self.page_index - 1)

        # Reset the cursor to the top, this is more pleasant than seeing the
        # cursor on the bottom of the screen after paging.
        self.cursor_index = 0
        self.top_offset = 0

    def move_page_down(self):
        """
        Move the page down by making the bottom item on the screen the top item.

        Note that the way that paging is programmed, there is no way to ensure
        that they are excact inverse operations. Pressing page down followed by
        page up may put you in a different location than where you started at.
        """
        self._content.get(self.page_index + self._n_subwindows)
        if self._n_subwindows > 1:
            self._align_top(self.page_index + self._n_subwindows - 1)
        else:
            self._align_top(self.page_index + self._n_subwindows)

    def move_page_top(self):
        """
        Jump to the top of the screen.
        """
        self._align_top(self._content.range[0])

    def move_page_bottom(self):
        """
        Jump to the last item index that has been loaded so far. Because PRAW
        loads items lazily, we can't know for sure where the *true* last
        comment/submission is, so we go down as far as we have seen before.
        """
        self._align_bottom(self._content.range[1])

    def _align_top(self, index):
        """
        Set the page paramaters so that the given index lines up with the top
        of the screen. We also try to draw to the bottom of the screen to make
        sure that there is enough content to fill up the page.
        """
        if index <= 0:
            # If we're at the very top of the content it doesn't matter if
            # there's enough text to fill the page
            self.page_index = index
            self.cursor_index = 0
            self.top_offset = 0
            return

        available_rows = self._n_rows
        content_gen = self._content.iterate(index, 1, self._n_cols - 2)
        for i, data in enumerate(content_gen):
            if data['n_rows'] >= available_rows:
                self.page_index = index
                self.cursor_index = 0
                self.top_offset = 0
                break

            available_rows -= data['n_rows']
        else:
            # Need to flip the alignment so the page fills up
            self._align_bottom(self._content.range[1])
            self.cursor_index = index - self.page_index

    def _align_bottom(self, index):
        """
        Set the page paramaters so that the given index lines up with the
        bottom of the screen. To do this we need to backtrack - start at the
        bottom of the screen and fill it up with content until we discover
        where the top should be.
        """
        available_rows = self._n_rows
        content_gen = self._content.iterate(index, -1, self._n_cols - 2)
        for i, data in enumerate(content_gen):
            if data['n_rows'] >= available_rows:
                # We have determined the first item on the page
                self.page_index = index - i
                self.cursor_index = i
                # Throw out the top offset if only one item can fit on the
                # screen, in that case it's better to draw from the top down
                self.top_offset = data['n_rows'] - available_rows if i else 0
                break

            # Add the current item and continue moving up the page
            available_rows -= data['n_rows']
        else:
            # Need to flip the alignment so the page fills up
            # TODO: Don't want this to pop up when scrolling
            self._align_top(self._content.range[0])
            self.cursor_index = index