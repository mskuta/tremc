# -*- coding: utf-8 -*-

########################################################################
# This program is free software: you can redistribute it and/or modify #
# it under the terms of the GNU General Public License as published by #
# the Free Software Foundation, either version 3 of the License, or    #
# (at your option) any later version.                                  #
#                                                                      #
# This program is distributed in the hope that it will be useful,      #
# but WITHOUT ANY WARRANTY; without even the implied warranty of       #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the        #
# GNU General Public License for more details:                         #
# http://www.gnu.org/licenses/gpl-3.0.txt                              #
########################################################################

import argparse
import base64
import configparser
import curses
import curses.ascii
import datetime
import enum
import json
import locale
import netrc
import operator
import os
import re
import signal
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from subprocess import Popen, call
from textwrap import wrap

import geoip2.database
import pyperclip

locale.setlocale(locale.LC_ALL, '')
PROG = 'tremc'

# Global constants and constant configuration
class GConfig:
    VERSION = '0.9.3+mskuta1.1.0'

    TRNSM_VERSION_MIN = '1.90'
    TRNSM_VERSION_MAX = '3.0.0'
    RPC_VERSION_MIN = 8
    RPC_VERSION_MAX = 17

    STARTTIME = time.time()
    DEBUG = False
    ENCODING = locale.getpreferredencoding() or 'UTF-8'

    # error codes
    class errors(enum.IntEnum):
        CONNECTION_ERROR = 1
        JSON_ERROR = 2
        CONFIGFILE_ERROR = 3

    FILTERS_WITH_PARAM = ['tracker', 'regex', 'location', 'label', 'group']

    def __init__(self):
        default_config_path = xdg_config_home(PROG + '/settings.cfg')
        parser = argparse.ArgumentParser(description="%(prog)s " + self.VERSION,
                                         usage="(prog)s [options] [torrent] -- transmission-remote-args ...",
                                         epilog="Positional arguments are passed to transmission-remote. Use -- to separate from %(prog)s arguments")
        parser.add_argument("-v", "--version", action="version", version="%(prog)s " + self.VERSION,
                            help="Show version number and supported Transmission versions.")
        parser.add_argument("-c", "--connect", action="store", dest="connection", default="",
                            help="Point to the server using pattern [username:password@]host[:port]/[path]")
        parser.add_argument("-s", "--ssl", action="store_true", dest="ssl", default=False,
                            help="Connect to Transmission using SSL.")
        parser.add_argument("-f", "--config", action="store", dest="configfile", default=default_config_path,
                            help="Path to configuration file.")
        parser.add_argument("--create-config", action="store_true", default=False,
                            help="Create configuration file CONFIGFILE with default values.")
        parser.add_argument("-l", "--list-actions", action="store_true", dest='listactions', default=False,
                            help="List available actions for key mapping.")
        parser.add_argument("-k", "--list-keys", action="store_true", dest='listkeys', default=False,
                            help="List available key names for key mapping.")
        parser.add_argument("-n", "--netrc", action="store_true", dest="use_netrc", default=False,
                            help="Get authentication info from your ~/.netrc file.")
        parser.add_argument("-X", "--skip-version-check", "--permissive", action="store_true", dest="PERMISSIVE", default=False,
                            help="Proceed even if the running transmission daemon seems incompatible, or the terminal is too small.")
        parser.add_argument("-p", "--profile", action="store", dest="profile",
                            help="Select profile to use.")
        parser.add_argument("-r", "--reverse-dns", action="store_true", dest="rdns", default=False,
                            help="Toggle reverse DNS peers addresses.")
        parser.add_argument("-d", "--debug", action="store", dest="DEBUG", nargs='?', default=False,
                            help="Enable debugging messages.")
        parser.add_argument('transmissionremote_args', nargs='*', metavar='A',
                            help="Torrent files to add using transmission-remote")
        cmd_args = parser.parse_args()

        for i in vars(cmd_args).keys():
            setattr(self, i, vars(cmd_args)[i])

        if self.DEBUG is None:
            self.DEBUG = True
            self.debug_file = sys.stderr
        elif isinstance(self.DEBUG, str):
            try:
                self.debug_file = open(self.DEBUG, "a", buffering=1)
            except OSError:
                pass

        if not self.create_config and not os.path.isfile(self.configfile) and '/' not in self.configfile:
            if os.path.isfile(xdg_config_home(PROG + '/' + self.configfile)):
                self.configfile = xdg_config_home(PROG + '/' + self.configfile)
            elif os.path.isfile(xdg_config_home(PROG + '/' + self.configfile + '.cfg')):
                self.configfile = xdg_config_home(PROG + '/' + self.configfile + '.cfg')
        self.configfile = self.configfile
        config.read(self.configfile)
        self.history_file = ''
        if PROG in os.path.dirname(self.configfile):
            self.history_file = os.path.join(os.path.dirname(self.configfile), 'history.json')
        elif PROG in os.path.basename(self.configfile):
            self.history_file = self.configfile.rsplit(PROG, 1)[0] + PROG + '-history.json'
        if config.has_option('Misc', 'geoip_database'):
            self.geoip_database = config.get('Misc', 'geoip_database')
        else:
            self.geoip_database = config.get('Misc', 'geoip2_database', fallback='')

        self.rdns = self.rdns ^ config.getboolean('Misc', 'rdns', fallback=False)

        # Handle connection details
        self.host = config.get('Connection', 'host', fallback='localhost')
        self.port = config.getint('Connection', 'port', fallback=9091)
        self.path = config.get('Connection', 'path', fallback='/transmission/rpc')
        self.username = config.get('Connection', 'username', fallback='')
        self.password = config.get('Connection', 'password', fallback='')
        un_pw = os.environ.get("TR_AUTH")
        if un_pw:
            self.username = un_pw.split(":")[0]
            self.password = ":".join(un_pw.split(":")[1:])
        if self.use_netrc:
            self.username, self.password = read_netrc(hostname=self.host)
        if self.connection:
            try:
                if self.connection.count('@') == 1:
                    auth, self.connection = self.connection.split('@')
                    if auth.count(':') == 1:
                        self.username, self.password = auth.split(':')
                if self.connection.count(':') == 1:
                    self.host, port = self.connection.split(':')
                    if port.count('/') >= 1:
                        port, self.path = port.split('/', 1)
                    self.port = int(port)
                else:
                    self.host = self.connection
                self.ssl = False # Don't use ssl from config file if given connection info on command line.
            except ValueError:
                exit_prog("Wrong connection pattern: %s\n" % self.connection)
        self.ssl = self.ssl | config.getboolean('Connection', 'ssl', fallback=False)
        url = '%s:%d/%s' % (self.host, self.port, self.path)
        url = url.replace('//', '/')   # double-/ doesn't work for some reason
        self.url = 'https://%s' % url if self.ssl else 'http://%s' % url

        if self.create_config:
            config.set('Connection', 'host', self.host)
            config.set('Connection', 'port', str(self.port))
            config.set('Connection', 'path', self.path)
            config.set('Connection', 'username', self.username)
            config.set('Connection', 'password', self.password)
            create_config(self.configfile, self.connection)

        self.sort_options = [
            ('name', '_Name'), ('addedDate', '_Age'), ('percentDone', '_Progress'),
            ('seeders', '_Seeds'), ('leechers', 'Lee_ches'), ('sizeWhenDone', 'Si_ze'),
            ('status', 'S_tatus'), ('uploadedEver', 'Up_loaded'),
            ('rateUpload', '_Upload Speed'), ('rateDownload', '_Download Speed'),
            ('uploadRatio', '_Ratio'), ('peersConnected', 'P_eers'),
            ('downloadDir', 'L_ocation'), ('mainTrackerDomain', 'Trac_ker'),
            ('queuePosition', '_Queue Position'),
            ('activityDate', 'Last activit_y'),
            ('eta', 'Time Le_ft'),
            ('reverse', 'Re_verse') ]
        self.file_sort_options = [
            ('name', '_Name'), ('progress', '_Progress'),
            ('length', 'Si_ze'), ('bytesCompleted', '_Downloaded'),
            ('none', '_Torrent order'),
            ('reverse', 'Re_verse')
        ]
        self.filters = [[{}]]
        self.filters[0][0]['name'] = config.get('Filtering', 'filter', fallback='')
        self.filters[0][0]['inverse'] = config.getboolean('Filtering', 'invert', fallback=False)
        self.sort_orders = parse_sort_str(config.get('Sorting', 'order', fallback=''), [x[0] for x in self.sort_options])
        self.file_sort_key = 'name'
        self.file_sort_reverse = False
        self.filters[0][0]['regex'] = ''
        self.filters[0][0]['tracker'] = ''
        self.filters[0][0]['location'] = ''
        self.histories = load_history(self.history_file)
        self.tlist_item_height = config.getint('Misc', 'lines_per_torrent')
        self.narrow_threshold = config.getint('Misc', 'narrow_threshold', fallback=73)
        self.torrentname_is_progressbar = config.getboolean('Misc', 'torrentname_is_progressbar')
        self.file_viewer = config.get('Misc', 'file_viewer')
        self.file_open_in_terminal = config.getboolean('Misc', 'file_open_in_terminal')
        self.view_selected = config.getboolean('Misc', 'view_selected', fallback=False)
        self.torrent_numbers = config.getboolean('Misc', 'torrent_numbers', fallback=False)
        self.profiles = parse_config_profiles(config, [x[0] for x in self.sort_options])

        try:
            self.selected_file_attr = curses.A_BOLD + curses.A_ITALIC
        except AttributeError:
            self.selected_file_attr = curses.A_BOLD

        self.actions = {
            # First in list: 0=all 1=list 2=details 3=files 4=tracker 16=movement
            # +256 for RPC>=14, +512 for RPC>=16, +1024 for RPC>=17
            'list_key_bindings': [0, ['F1', '?'], 'List key bindings'],
            'quit_now': [0, ['^w'], 'Quit immediately'],
            'quit': [1, ['q'], 'Quit'],
            'leave_details': [2, ['BACKSPACE', 'q'], 'Back to torrent list'],
            'go_back_or_unfocus': [2, ['ESC', 'BREAK'], 'Unfocus or back to torrent list'],
            'daemon_quit': [0, ['X'], 'Ask daemon to quit'],
            'options_dialog': [0, ['O'], PROG + ' options menu'],
            'server_options_dialog': [1, ['o'], 'Server options menu'],
            'toggle_compact_torrentlist': [1, ['C'], 'Cycle torrent line height'],
            'toggle_torrent_numbers': [1, [], 'Toggle torrent number in list'],
            'turtle_mode': [1, ['t'], 'Toggle turtle mode'],
            'unmapped_actions': [0, '`', 'Show actions not mapped to keys'],
            'global_upload': [0, ['u'], 'Set global upload'],
            'global_download': [0, ['d'], 'Set global download limit'],
            'torrent_upload': [0, ['U'], 'Set torrent maximum upload rate'],
            'torrent_download': [0, ['D'], 'Set torrent maximum download rate'],
            'group_upload': [0, [], 'Set group maximum upload rate'],
            'group_download': [0, [], 'Set group maximum download rate'],
            'seed_ratio': [0, ['L'], 'Set seed ratio limit for focused torrent'],
            'bandwidth_priority_inc': [0, ['+'], 'Increase torrent bandwidth priority'],
            'bandwidth_priority_dec': [0, ['-'], 'Decrease torrent bandwidth priority'],
            'honors_limits': [0, ['*'], 'Toggle torrent honors session limits'],
            'pause_unpause_torrent': [0, ['p'], 'Pause/Unpause torrent'],
            'pause_unpause_all_torrent': [0, ['P'], 'Pause/Unpause all torrents'],
            'start_now_torrent': [0, ['N'], 'Start torrent now'],
            'verify_torrent': [0, ['v', 'y'], 'Verify torrent'],
            'move_torrent': [0, ['m'], 'Move torrent'],
            'rename_torrent_selected_file': [0, ['F'], 'Rename torrent/file'],
            'reannounce_torrent': [0, ['n'], 'Reannounce torrent'],
            'show_stats': [0, ['S'], 'Show upload/download stats'],
            'remove': [1, ['DC', 'r'], 'Remove selected/focused torrents, keeping content'],
            'remove_focused': [1, [], 'Remove focused torrent keeping content'],
            'remove_selected': [1, ['^r'], 'Remove selected torrents'],
            'remove_data': [0, [], 'Remove selected/focused torrents and content'],
            'remove_focused_data': [0, ['SDC', 'R'], 'Remove torrent and content'],
            'remove_selected_data': [1, [], 'Remove selected torrents and content'],
            'copy_magnet_link': [0, ['M'], 'Copy Magnet Link to the System Clipboard'],
            'remove_labels': [512, ['^l'], 'Remove labels'],
            'add_label': [512, ['b'], 'Add label'],
            'set_labels': [512, ['B'], 'Set labels'],
            'set_group': [1024, [], 'Set group'],
            'group_get': [1024, [], 'Get group list'],
            'move_queue_down': [257, ['J'], 'Move torrent down in queue'],
            'move_queue_up': [257, ['K'], 'Move torrent up in queue'],
            'profile_menu': [1, ['e'], 'Profile menu'],
            'save_profile': [1, ['E'], 'Save profile'],
            'search_torrent': [1, ['/'], 'Find torrent'],
            'search_torrent_regex': [1, ['.'], 'Find torrents matching regular expression'],
            'search_torrent_fulltext': [1, [], 'Find torrent (full text)'],
            'search_torrent_regex_fulltext': [1, [], 'Find torrents matching regular expression (full text)'],
            'set_filter': [1, ['f'], 'Set filter'],
            'add_filter': [1, ['T'], 'Add filter'],
            'add_filter_line': [1, ['^t'], 'Add filter line'],
            'edit_filters': [1, ['I'], 'Edit list of filters'],
            'invert_filters': [1, ['~'], 'Reverse filters'],
            'show_torrent_sort_order_menu': [1, ['s'], 'Sort torrent list'],
            'select_unselect_torrent': [1, ['SPACE'], 'Select/unselect torrent'],
            'select_unselect_torrents': [1, ['A'], 'Select/Deselect all torrents'],
            'invert_selection_torrents': [1, ['i'], 'Invert torrent selection'],
            'select_search_torrent': [1, [','], 'Select torrents matching pattern'],
            'select_search_torrent_regex': [1, ['<'], 'Select torrents matching regex'],
            'select_search_torrent_fulltext': [1, [], 'Select torrents matching pattern (full text)'],
            'select_search_torrent_regex_fulltext': [1, [], 'Select torrents matching regex (full text)'],
            'enter_details': [1, ['ENTER', 'RIGHT', 'l'], 'Enter torrent details view'],
            'add_torrent': [1, ['a'], 'Add torrent'],
            'add_torrent_paused': [1, ['^a'], 'Add torrent paused'],
            'unfocus_torrent': [1, ['ESC', 'BREAK'], 'Unfocus torrent'],
            'tab_overview': [2, ['o'], 'Jump to overview'],
            'tab_files': [2, ['f'], 'Jump to file list'],
            'tab_peers': [2, ['e'], 'Jump to peer list'],
            'tab_trackers': [2, ['t'], 'Jump to tracker list'],
            'tab_chunks': [2, ['c'], 'Jump to chunk list'],
            'next_details': [2, ['TAB'], 'Next details tab'],
            'prev_details': [2, ['BTAB'], 'Previous details tab'],
            'file_priority_or_switch_details_next': [2, ['RIGHT', 'l'], 'Raise file priority or Previous tab'],
            'file_priority_or_switch_details_prev': [2, ['LEFT', 'h'], 'Lower file priority or Previous tab'],
            'add_tracker_or_select_all_files': [2, ['a'], 'Select/Deselect all files or add torrent'],
            'view_file': [3, ['ENTER'], 'View file'],
            'view_file_command': [3, ['|'], 'Run command on file'],
            'move_to_next_directory': [3, ['J'], 'Next diectory'],
            'move_to_previous_directory': [3, ['K'], 'Previous directory'],
            'show_file_sort_order_menu': [3, ['s'], 'Sort file list'],
            'visual_select_files': [3, ['V'], 'Visually select files'],
            'select_search_file': [3, [','], 'Select files matching pattern'],
            'select_files_dir': [3, ['A'], 'Select/Deselect directory'],
            'search_file': [3, ['/'], 'Search file list'],
            'rename_dir': [3, ['C'], 'Rename directory inside torrent'],
            'select_search_file_regex': [3, ['<'], 'Select files matching regex'],
            'search_file_regex': [3, ['.'], 'Find files matching regex'],
            'invert_selection_files': [3, ['i'], 'Invert selection'],
            'select_file': [3, ['SPACE'], 'Select/unselect file'],
            'file_info': [3, ['x'], 'Show file info'],
            'remove_tracker': [4, ['DC', 'r'], 'Remove tracker'],
            'page_up': [16, ['PPAGE', '^b'], 'Page Up'],
            'page_down': [16, ['NPAGE', '^f'], 'Page Down'],
            'line_up': [16, ['UP', 'k', '^p'], 'Up'],
            'line_down': [16, ['DOWN', 'j', '^n'], 'Down'],
            'go_home': [16, ['HOME', 'g'], 'Home'],
            'go_end': [16, ['END', 'G'], 'End'],
        }
        self.keys = [x for x in dir(K) if x[0] != '_'] + \
                    [x[4:] for x in dir(curses) if x[:4] == 'KEY_']
        exit = False
        if self.listactions:
            list_actions(self.actions)
            exit = True
        if self.listkeys:
            list_keys()
            exit = True
        if exit:
            sys.exit(0)

    def init_colors(self, config):
        colors = {
            'title_seed': 'bg:green,fg:black',
            'title_download': 'bg:blue,fg:black',
            'title_idle': 'bg:cyan,fg:black',
            'title_verify': 'bg:magenta,fg:black',
            'title_paused': 'bg:default,fg:default',
            'title_paused_done': 'title_paused',
            'title_error': 'bg:red,fg:default',
            'title_seed_incomp': 'a:r',
            'title_download_incomp': 'a:r',
            'title_idle_incomp': 'a:r',
            'title_verify_incomp': 'a:r',
            'title_paused_incomp': 'a:r',
            'title_paused_done_incomp': 'title_paused_incomp',
            'title_error_incomp': 'a:r',
            'title_other': 'bg:default,fg:default',
            'download_rate': 'bg:default,fg:blue,a:b',
            'upload_rate': 'bg:black,fg:red,a:b',
            'eta+ratio': 'bg:default,fg:default,a:b',
            'filter_status': 'bg:red,fg:black',
            'sort_status': 'bg:red,fg:black',
            'multi_filter_status': 'bg:blue,fg:black',
            'dialog': 'bg:default,fg:default,a:rb',
            'dialog_important': 'bg:default,fg:red,a:r',
            'dialog_text': 'dialog,a:*r',
            'dialog_text_important': 'dialog_important,a:*r',
            'menu_focused': 'dialog,a:*r',
            'file_prio_high': 'fg:red,bg:default',
            'file_prio_normal': 'fg:default,bg:default',
            'file_prio_low': 'fg:yellow,bg:default',
            'file_prio_off': 'fg:blue,bg:default',
            'top_line': 'a:r',
            'bottom_line': 'a:r',
            'chunk_have': 'a:r',
            'chunk_dont_have': '',
        }
        colors.update(config)
        self.colors = dict()
        self.term_has_colors = curses.has_colors()
        curses.start_color()
        if self.term_has_colors:
            curses.use_default_colors()
        for name in list(colors.keys()):
            self.colors[name] = self._parse_color_pair(colors[name])
            if self.term_has_colors:
                curses.init_pair(self.colors[name]['ind'],
                                 self.colors[name]['fg'],
                                 self.colors[name]['bg'])

    def _parse_color_pair(self, pair):
        attrs = {
            'r': curses.A_REVERSE,
            'b': curses.A_BOLD,
            'i': curses.A_ITALIC,
            'k': curses.A_BLINK,
            'd': curses.A_DIM,
            'u': curses.A_UNDERLINE,
        }
        parts = pair.split(',')
        bg_name = [x for x in parts if x[:3] == 'bg:'][0].split(':')[1].upper() if 'bg:' in pair else None
        fg_name = [x for x in parts if x[:3] == 'fg:'][0].split(':')[1].upper() if 'fg:' in pair else None
        attrs_name = next((x[2:] for x in parts if x[:2] == 'a:'), '')
        element_copy = next((x for x in parts if x in self.colors), None)
        color_pair = {'ind': len(list(self.colors.keys())) + 1}
        color_pair['bg'] = -1
        color_pair['fg'] = -1
        color_pair['at'] = curses.A_NORMAL

        if element_copy:
            color_pair['bg'] = self.colors[element_copy]['bg']
            color_pair['fg'] = self.colors[element_copy]['fg']
            color_pair['at'] = self.colors[element_copy]['at']
        if bg_name:
            if bg_name == 'DEFAULT':
                color_pair['bg'] = -1
            else:
                color_pair['bg'] = getattr(curses, 'COLOR_' + bg_name, -1)
        if fg_name:
            if fg_name == 'DEFAULT':
                color_pair['fg'] = -1
            else:
                color_pair['fg'] = getattr(curses, 'COLOR_' + fg_name, -1)
        for i in range(len(attrs_name)):
            if attrs_name[i] == '0':
                color_pair['at'] = curses.A_NORMAL
            if attrs_name[i] in attrs:
                if i > 0 and attrs_name[i-1] == '-':
                    color_pair['at'] = color_pair['at'] & ~attrs[attrs_name[i]]
                elif i > 0 and attrs_name[i-1] == '*':
                    color_pair['at'] = color_pair['at'] ^ attrs[attrs_name[i]]
                else:
                    color_pair['at'] = color_pair['at'] | attrs[attrs_name[i]]
        return color_pair

    def element_attr(self, name, st=False):
        try:
            if st:
                name = 'st_' + name
                if name not in self.colors:
                    return curses.A_REVERSE
            return curses.color_pair(self.colors[name]['ind']) + self.colors[name]['at']
        except:
            # This only happens if when a bug manifests, but it's better to not
            # crach even in this situation.
            pdebug('element_attr', name, st)
            return 0


class Keys:
    TAB = 9
    LF = 10
    CR = 13
    ESC = 27
    SPACE = 32
    EXCLAMATION = 33
    QUOT = 34
    HASH = 35
    DOLLAR = 36
    PERCENT = 37
    AMPERSAND = 38
    APOSTROPHE = 39
    LPAREN = 40
    RPAREN = 41
    STAR = 42
    PLUS = 43
    COMMA = 44
    MINUS = 45
    DOT = 46
    SLASH = 47
    COLON = 58
    SEMICOLON = 59
    LT = 60
    EQUAL = 61
    GT = 62
    QUES = 63
    AT = 64
    LBRACKET = 91
    BACKSLASH = 92
    RBRACKET = 93
    CARET = 94
    UL = 95
    BACKTICK = 96
    LBRACE = 123
    PIPE = 124
    RBRACE = 125
    TILDE = 126
    DEL = 127
    def __init__(self):
        for i in range(1, 27):
            setattr(self, chr(64 + i), 64 + i)
            setattr(self, chr(64 + i) + '_', i)
            setattr(self, chr(96 + i), 96 + i)
        for i in range(0, 10):
            setattr(self, 'n' + str(i), ord('0') + i)

K = Keys()

def pdebug(*argv):
    if gconfig.DEBUG:
        print(time.time() - gconfig.STARTTIME, ": ", *argv, file=gconfig.debug_file, flush=True)


# define config defaults
config = configparser.ConfigParser()
config.optionxform = lambda option: option
config.add_section('Connection')
config.add_section('Sorting')
config.set('Sorting', 'order', 'name')
config.add_section('Filtering')
config.set('Filtering', 'filter', '')
config.set('Filtering', 'invert', 'False')
config.add_section('Misc')
config.set('Misc', 'lines_per_torrent', '2')
config.set('Misc', 'torrentname_is_progressbar', 'True')
config.set('Misc', 'file_viewer', 'xdg-open %%s')
config.set('Misc', 'file_open_in_terminal', 'True')
config.add_section('Colors')

class Normalizer:
    def __init__(self):
        self.values = {}

    def add(self, key, value, max_len):
        if key not in list(self.values.keys()):
            self.values[key] = [float(value)]
        else:
            if len(self.values[key]) >= max_len:
                self.values[key].pop(0)
            self.values[key].append(float(value))
        return self.get(key)

    def get(self, key):
        if key not in list(self.values.keys()):
            return 0.0
        return sum(self.values[key]) / len(self.values[key])


class TransmissionRequest:
    """Handle communication with Transmission server."""

    def __init__(self, url, method=None, tag=None, arguments=None, server=None):
        """server is not really optional"""
        self.url = url
        self.open_request = None
        self.last_update = 0
        self.server = server
        if method and tag:
            self.set_request_data(method, tag, arguments)

    def set_request_data(self, method, tag, arguments=None):
        request_data = {'method': method, 'tag': tag}
        if arguments:
            request_data['arguments'] = arguments
        self.http_request = urllib.request.Request(self.url, bytes(json.dumps(request_data), gconfig.ENCODING))

    def send_request(self):
        """Ask for information from server OR submit command."""
        try:
            if self.server.session_id:
                self.http_request.add_header('X-Transmission-Session-Id', self.server.session_id)
            self.open_request = urllib.request.urlopen(self.http_request)
        except AttributeError:
            # request data (http_request) isn't specified yet -- data will be available on next call
            pass

        # authentication
        except urllib.error.HTTPError as e:
            try:
                msg = html2text(str(e.read()))
            except Exception:
                msg = str(e)

            # extract session id and send request again
            m = re.search(r'X-Transmission-Session-Id:\s*(\w+)', msg)
            try:
                self.server.session_id = m.group(1)
                self.send_request()
            except AttributeError:
                exit_prog(str(msg) + "\n", gconfig.errors.CONNECTION_ERROR)

        except urllib.error.URLError as msg:
            exit_prog("Cannot connect to %s: %s" % (self.http_request.host, msg.reason), gconfig.errors.CONNECTION_ERROR)

    def get_response(self):
        """Get response to previously sent request."""

        if self.open_request is None:
            return {'result': 'no open request'}
        response = b''
        while True:
            try:
                chunk = self.open_request.read()
            except ConnectionResetError:
                return {'result': 'connection reset by peer'}
            except Exception as e:
                pdebug(str(e))
                return {'result': 'Exception'}
            if not chunk:
                break
            response += chunk

        try:
            data = json.loads(response.decode("utf-8"))
        except ValueError:
            exit_prog("Cannot parse response: %s\n" % response, gconfig.errors.JSON_ERROR)
        self.open_request = None
        return data


# End of Class TransmissionRequest


class Transmission:
    """Higher level of data exchange"""
    STATUS_STOPPED = 0   # Torrent is stopped
    STATUS_CHECK_WAIT = 1   # Queued to check files
    STATUS_CHECK = 2   # Checking files
    STATUS_DOWNLOAD_WAIT = 3   # Queued to download
    STATUS_DOWNLOAD = 4   # Downloading
    STATUS_SEED_WAIT = 5   # Queued to seed
    STATUS_SEED = 6   # Seeding

    TAG_TORRENT_LIST = 7
    TAG_TORRENT_DETAILS = 77
    TAG_SESSION_STATS = 21
    TAG_SESSION_GET = 22
    TAG_SESSION_CLOSE = 23
    TAG_GROUP_GET = 80

    LIST_FIELDS = ['id', 'name', 'downloadDir', 'status', 'trackerStats', 'desiredAvailable',
                   'rateDownload', 'rateUpload', 'eta', 'uploadRatio',
                   'sizeWhenDone', 'haveValid', 'haveUnchecked', 'addedDate',
                   'uploadedEver', 'error', 'errorString', 'recheckProgress',
                   'peersConnected', 'uploadLimit', 'downloadLimit',
                   'uploadLimited', 'downloadLimited', 'bandwidthPriority',
                   'peersSendingToUs', 'peersGettingFromUs', 'totalSize',
                   'seedRatioLimit', 'seedRatioMode', 'isPrivate', 'magnetLink',
                   'honorsSessionLimits', 'metadataPercentComplete',
                   'activityDate',
                   ]

    DETAIL_FIELDS = ['files', 'priorities', 'wanted', 'peers', 'trackers',
                     'dateCreated', 'startDate', 'doneDate',
                     'leftUntilDone', 'comment', 'creator',
                     'hashString', 'pieceCount', 'pieceSize', 'pieces',
                     'downloadedEver', 'corruptEver', 'peersFrom'] + LIST_FIELDS

    def __init__(self, url, username, password):
        self.url = url
        self.session_id = 0

        if username and password:
            password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            password_mgr.add_password(None, url, username, password)
            authhandler = urllib.request.HTTPBasicAuthHandler(password_mgr)
            opener = urllib.request.build_opener(authhandler)
            urllib.request.install_opener(opener)

        # check rpc version
        request = TransmissionRequest(url, 'session-get', self.TAG_SESSION_GET, server=self)
        request.send_request()
        response = request.get_response()

        self.rpc_version = response['arguments']['rpc-version']
        self.version = response['arguments']['version'].split()[0]

        # rpc version too old?
        version_error = "Unsupported Transmission version: " + str(response['arguments']['version']) + \
            " -- RPC protocol version: " + str(response['arguments']['rpc-version']) + "\n"
        skip_msg = "Proceeding anyway because of --skip-version-check.\n"

        min_msg = "Please install Transmission version " + gconfig.TRNSM_VERSION_MIN + " or higher.\n"
        alternative_msg = "Alternatively start the program with the option '--skip-version-check', '--permissive', or '-X' to inhibit version checking\n"
        try:
            if response['arguments']['rpc-version'] < gconfig.RPC_VERSION_MIN:
                if gconfig.PERMISSIVE:
                    pdebug(version_error + skip_msg)
                else:
                    exit_prog(version_error + min_msg + alternative_msg)
        except KeyError:
            exit_prog(version_error + min_msg)

        # rpc version too new?
        if response['arguments']['rpc-version'] > gconfig.RPC_VERSION_MAX:
            if gconfig.PERMISSIVE:
                pdebug(version_error + skip_msg)
            else:
                exit_prog(version_error + "Please install Transmission version " + gconfig.TRNSM_VERSION_MAX + " or lower.\n" + alternative_msg)

        # setup compatibility to Transmission <2.40
        if self.rpc_version < 14:
            Transmission.STATUS_CHECK_WAIT = 1 << 0
            Transmission.STATUS_CHECK = 1 << 1
            Transmission.STATUS_DOWNLOAD_WAIT = 1 << 2
            Transmission.STATUS_DOWNLOAD = 1 << 2
            Transmission.STATUS_SEED_WAIT = 1 << 3
            Transmission.STATUS_SEED = 1 << 3
            Transmission.STATUS_STOPPED = 1 << 4

        # Queue was implemented in Transmission v2.4
        if self.rpc_version >= 14:
            self.LIST_FIELDS.append('queuePosition')
            self.DETAIL_FIELDS.append('queuePosition')
        else:
            gconfig.sort_options.remove(('queuePosition', '_Queue Position'))
            if gconfig.sort_orders[0]['name'] == 'queuePosition':
                # Use default sort if set to invalid queuePosition.
                gconfig.sort_orders = [{'name': 'name', 'reverse': False}]

        if self.rpc_version >= 16:
            self.LIST_FIELDS.append('labels')
            self.DETAIL_FIELDS.append('labels')
            self.LIST_FIELDS.append('group')
            self.DETAIL_FIELDS.append('group')

        # set up request list
        self.requests = {'torrent-list':
                         TransmissionRequest(url, 'torrent-get', self.TAG_TORRENT_LIST, {'fields': self.LIST_FIELDS}, server=self),
                         'session-stats':
                             TransmissionRequest(url, 'session-stats', self.TAG_SESSION_STATS, 21, server=self),
                         'session-get':
                             TransmissionRequest(url, 'session-get', self.TAG_SESSION_GET, server=self),
                         'torrent-details':
                             TransmissionRequest(url, server=self)}

        self.torrent_cache = []
        self.trackers = set()
        self.locations = set()
        self.labels = set()
        self.groups = set()
        self.status_cache = dict()
        self.torrent_details_cache = dict()
        self.peer_progress_cache = dict()
        self.hosts_cache = dict()

        self.geo_ips_cache = dict()
        try:
            self.geo_ip = geoip2.database.Reader(gconfig.geoip_database)
        except Exception:
            self.geo_ip = None

        # make sure there are no undefined values
        self.wait_for_torrentlist_update()
        self.requests['torrent-details'] = TransmissionRequest(self.url, server=self)

    def update(self, delay, tag_waiting_for=0):
        """Maintain up-to-date data."""

        tag_waiting_for_occurred = False

        for request in list(self.requests.values()):
            if time.time() - request.last_update >= delay:
                request.last_update = time.time()
                response = request.get_response()

                if response['result'] == 'no open request':
                    request.send_request()

                elif response['result'] == 'success':
                    tag = self.parse_response(response)
                    if tag == tag_waiting_for:
                        tag_waiting_for_occurred = True

        return tag_waiting_for_occurred if tag_waiting_for else None

    def parse_response(self, response):
        def get_main_tracker_domain(torrent):
            if torrent['trackerStats']:
                trackers = sorted(torrent['trackerStats'],
                                  key=operator.itemgetter('tier', 'id'))
                return urllib.parse.urlparse(trackers[0]['announce']).hostname
            # Trackerless torrents
            return "None"

        # response is a reply to torrent-get
        if response['tag'] == self.TAG_TORRENT_LIST or response['tag'] == self.TAG_TORRENT_DETAILS:
            for t in response['arguments']['torrents']:
                t['uploadRatio'] = round(float(t['uploadRatio']), 2)
                t['percentDone'] = percent(float(t['sizeWhenDone']),
                                           float(t['haveValid'] + t['haveUnchecked']))
                t['available'] = t['desiredAvailable'] + t['haveValid'] + t['haveUnchecked']
                if t['downloadDir'][-1] != '/':
                    t['downloadDir'] += '/'
                try:
                    t['seeders'] = max([x['seederCount'] for x in t['trackerStats']])
                    t['leechers'] = max([x['leecherCount'] for x in t['trackerStats']])
                except ValueError:
                    t['seeders'] = t['leechers'] = -1
                t['isIsolated'] = not self.can_has_peers(t)
                t['mainTrackerDomain'] = get_main_tracker_domain(t)
                if t['mainTrackerDomain']:
                    self.trackers.add(t['mainTrackerDomain'])
                self.locations.add(homedir2tilde(t['downloadDir']))
                if self.rpc_version >= 16:
                    for l in t['labels']:
                        self.labels.add(l)
                if self.rpc_version >= 17:
                    self.groups.add(t['group'])

            if response['tag'] == self.TAG_TORRENT_LIST:
                self.torrent_cache = response['arguments']['torrents']

            elif response['tag'] == self.TAG_TORRENT_DETAILS:
                # torrent list may be empty sometimes after deleting
                # torrents.  no idea why and why the server sends us
                # TAG_TORRENT_DETAILS, but just passing seems to help.(?)
                try:
                    if len(response['arguments']['torrents']) > 1:
                        self.torrent_details_cache = response['arguments']['torrents']
                    else:
                        torrent_details = response['arguments']['torrents'][0]
                        torrent_details['pieces'] = base64.decodebytes(bytes(torrent_details['pieces'], gconfig.ENCODING))
                        self.torrent_details_cache = torrent_details
                        self.upgrade_peerlist()
                except IndexError:
                    pass

        elif response['tag'] == self.TAG_SESSION_STATS:
            self.status_cache.update(response['arguments'])

        elif response['tag'] == self.TAG_SESSION_GET:
            self.status_cache.update(response['arguments'])

        return response['tag']

    def upgrade_peerlist(self):
        for index, peer in enumerate(self.torrent_details_cache['peers']):
            ip = peer['address']
            peerid = ip + self.torrent_details_cache['hashString']

            # make sure peer cache exists
            if peerid not in self.peer_progress_cache:
                self.peer_progress_cache[peerid] = {
                    'last_progress': peer['progress'],
                    'last_update': time.time(),
                    'download_speed': 0,
                    'time_left': 0
                }

            this_peer = self.peer_progress_cache[peerid]
            this_torrent = self.torrent_details_cache

            # estimate how fast a peer is downloading
            if peer['progress'] < 1:
                this_time = time.time()
                time_diff = this_time - this_peer['last_update']
                progress_diff = peer['progress'] - this_peer['last_progress']
                if this_peer['last_progress'] and progress_diff > 0 and time_diff > 5:
                    download_left = this_torrent['totalSize'] - \
                        (this_torrent['totalSize'] * peer['progress'])
                    downloaded = this_torrent['totalSize'] * progress_diff

                    this_peer['download_speed'] = \
                        norm.add(peerid + ':download_speed', downloaded / time_diff, 10)
                    this_peer['time_left'] = download_left / this_peer['download_speed']
                    this_peer['last_update'] = this_time

                # infrequent progress updates lead to increasingly inaccurate
                # estimates, so we go back to <guessing>
                elif time_diff > 60:
                    this_peer['download_speed'] = 0
                    this_peer['time_left'] = 0
                    this_peer['last_update'] = time.time()
                this_peer['last_progress'] = peer['progress']  # remember progress
            this_torrent['peers'][index].update(this_peer)

            # resolve and locate peer's ip
            if gconfig.rdns and ip not in self.hosts_cache:
                threading.Thread(target=reverse_dns, args=(self.hosts_cache, ip), daemon=True).start()
            if self.geo_ip and ip not in self.geo_ips_cache:
                try:
                    self.geo_ips_cache[ip] = self.geo_ip.country(ip).country.iso_code
                except Exception:
                    self.geo_ips_cache[ip] = '?'

    def get_rpc_version(self):
        return self.rpc_version

    def get_global_stats(self):
        return self.status_cache

    def get_torrent_list(self, sort_orders):
        def sort_value(value):
            # Always return a string, so everything is comparable
            if isinstance(value, (int, float)):
                # 20 digits should be quite enough for anything (for now)
                return "%027.6f" % value
            elif isinstance(value, str):
                return value.lower()
            else:
                return str(value)
        try:
            for sort_order in sort_orders:
                self.torrent_cache.sort(key=lambda x: sort_value(x[sort_order['name']]),
                                        reverse=sort_order['reverse'])
        except IndexError:
            return []
        return self.torrent_cache

    def get_torrent_by_id(self, t_id):
        i = 0
        while self.torrent_cache[i]['id'] != t_id:
            i += 1
        return self.torrent_cache[i] if self.torrent_cache[i]['id'] == t_id else None

    def get_torrent_details(self):
        return self.torrent_details_cache

    def set_torrent_details_id(self, t_id):
        if isinstance(t_id, int) and t_id < 0:
            self.requests['torrent-details'] = TransmissionRequest(self.url, server=self)
        else:
            self.requests['torrent-details'].set_request_data('torrent-get', self.TAG_TORRENT_DETAILS,
                                                              {'ids': t_id, 'fields': self.DETAIL_FIELDS})

    def get_hosts(self):
        return self.hosts_cache

    def get_geo_ips(self):
        return self.geo_ips_cache

    def get_free_space(self):
        request = TransmissionRequest(self.url, 'session-get', self.TAG_SESSION_GET, server=self)
        request.send_request()
        response = request.get_response()
        path = response['arguments']['download-dir']

        request = TransmissionRequest(self.url, 'free-space', 1, {'path': path}, server=self)
        request.send_request()
        response = request.get_response()
        if 'size-bytes' in response['arguments']:
            free = response['arguments']['size-bytes']
        else:
            free = 0

        return free  # free space in bytes

    def set_option(self, option_name, option_value):
        request = TransmissionRequest(self.url, 'session-set', 1, {option_name: option_value}, server=self)
        request.send_request()
        self.wait_for_status_update()

    # torrent_id is -1 for global or a non-empty list of ids
    def set_rate_limit(self, direction, new_limit, torrent_id=-1, group = None):
        data = dict()
        if new_limit <= -1:
            new_limit = None
            limit_enabled = False
        else:
            limit_enabled = True

        if group is not None:
            request_type = 'group-set'
            data['name'] = group
            data['speed-limit-' + direction] = new_limit
            data['speed-limit-' + direction + '-enabled'] = limit_enabled
        elif torrent_id == -1:
            request_type = 'session-set'
            data['speed-limit-' + direction] = new_limit
            data['speed-limit-' + direction + '-enabled'] = limit_enabled
        else:
            request_type = 'torrent-set'
            data['ids'] = torrent_id
            data[direction + 'loadLimit'] = new_limit
            data[direction + 'loadLimited'] = limit_enabled

        request = TransmissionRequest(self.url, request_type, 1, data, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def set_seed_ratio(self, ratio, ids=-1):
        data = dict()
        if ratio == -1:
            ratio = None
            mode = 0   # Use global settings
        elif ratio == 0:
            ratio = None
            mode = 2   # Seed regardless of ratio
        elif ratio >= 0:
            mode = 1   # Stop seeding at seedRatioLimit
        else:
            return

        data['ids'] = ids
        data['seedRatioLimit'] = ratio
        data['seedRatioMode'] = mode
        request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def toggle_honors_session_limits(self, torrent_ids):
        if torrent_ids:
            new_honors = not all([self.get_torrent_by_id(t)['honorsSessionLimits'] for t in torrent_ids])
            request = TransmissionRequest(self.url, 'torrent-set', 1,
                                          {'ids': torrent_ids, 'honorsSessionLimits': new_honors}, server=self)
            request.send_request()
            self.wait_for_torrentlist_update()

    def increase_bandwidth_priority(self, torrent_ids):
        if torrent_ids:
            current = min([self.get_torrent_by_id(t)['bandwidthPriority'] for t in torrent_ids])
            if current < 1:
                request = TransmissionRequest(self.url, 'torrent-set', 1,
                                              {'ids': torrent_ids, 'bandwidthPriority': current + 1}, server=self)
                request.send_request()
                self.wait_for_torrentlist_update()

    def decrease_bandwidth_priority(self, torrent_ids):
        if torrent_ids:
            current = max([self.get_torrent_by_id(t)['bandwidthPriority'] for t in torrent_ids])
            if current > -1:
                request = TransmissionRequest(self.url, 'torrent-set', 1,
                                              {'ids': torrent_ids, 'bandwidthPriority': current - 1}, server=self)
                request.send_request()
                self.wait_for_torrentlist_update()

    def move_queue(self, torrent_id, new_position):
        args = {'ids': [torrent_id]}
        if new_position in ('up', 'down', 'top', 'bottom'):
            method_name = 'queue-move-' + new_position
        elif isinstance(new_position, int):
            method_name = 'torrent-set'
            args['queuePosition'] = min(max(new_position, 0), len(self.torrent_cache) - 1)
        else:
            raise ValueError("Is not up/down/top/bottom/<number>: %s" % new_position)

        request = TransmissionRequest(self.url, method_name, 1, args, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def toggle_turtle_mode(self):
        self.set_option('alt-speed-enabled', not self.status_cache['alt-speed-enabled'])

    def add_torrent(self, location, paused=False):
        args = {'paused': paused}
        try:
            with open(location, 'rb') as fp:
                args['metainfo'] = base64.b64encode(fp.read()).decode()
        # If the file doesn't exist or we can't open it, then it is either a url or needs to
        # be open by the server
        except IOError:
            args['filename'] = location

        request = TransmissionRequest(self.url, 'torrent-add', 1, args, server=self)
        request.send_request()
        response = request.get_response()
        return response['result'] if response['result'] != 'success' else ''

    def daemon_quit(self):
        request = TransmissionRequest(self.url, 'session-close', self.TAG_SESSION_CLOSE, server=self)
        request.send_request()
        self.wait_for_update(self.TAG_SESSION_CLOSE)

    def stop_torrents(self, ids):
        request = TransmissionRequest(self.url, 'torrent-stop', 1, {'ids': ids}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def start_torrents(self, ids):
        request = TransmissionRequest(self.url, 'torrent-start', 1, {'ids': ids}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def start_now_torrent(self, ids):
        request = TransmissionRequest(self.url, 'torrent-start-now', 1, {'ids': ids}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def verify_torrent(self, ids):
        request = TransmissionRequest(self.url, 'torrent-verify', 1, {'ids': ids}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def reannounce_torrent(self, ids):
        request = TransmissionRequest(self.url, 'torrent-reannounce', 1, {'ids': ids}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def move_torrent(self, torrent_id, new_location):
        request = TransmissionRequest(self.url, 'torrent-set-location', 1,
                                      {'ids': torrent_id, 'location': new_location, 'move': True}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def remove_torrent(self, ids, data=False):
        request = TransmissionRequest(self.url, 'torrent-remove', 1, {'ids': ids, 'delete-local-data': data}, server=self)
        request.send_request()
        self.wait_for_torrentlist_update()

    def rename_torrent_file(self, t_id, path, newname):
        request = TransmissionRequest(self.url, 'torrent-rename-path', 1,
                                      {'ids': [t_id], 'path': path, 'name': newname}, server=self)
        request.send_request()
        response = request.get_response()
        return response['result']

    def set_group(self, ids, group):
        data = {
            'ids': ids,
            'group': group
        }
        request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
        request.send_request()
        response = request.get_response()
        return response['result'] if response['result'] != 'success' else ''

    def set_labels(self, ids, labels):
        data = {
            'ids': ids,
            'labels': labels
        }
        request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
        request.send_request()
        response = request.get_response()
        return response['result'] if response['result'] != 'success' else ''

    def add_label(self, ids, label):
        ret = ''
        for i in ids:
            t = self.get_torrent_by_id(i)
            if label not in t['labels']:
                data = {
                    'ids': [i],
                    'labels': t['labels'] + [label]
                }
                request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
                request.send_request()
                response = request.get_response()
                if ret == '':
                    ret = response['result'] if response['result'] != 'success' else ''
        return ret

    def add_torrent_tracker(self, t_id, tracker):
        data = {
            'ids': [t_id],
            'trackerAdd': [tracker]
        }
        request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
        request.send_request()
        response = request.get_response()
        return response['result'] if response['result'] != 'success' else ''

    def remove_torrent_tracker(self, t_id, tracker):
        data = {'ids': [t_id],
                'trackerRemove': [tracker]}
        request = TransmissionRequest(self.url, 'torrent-set', 1, data, server=self)
        request.send_request()
        response = request.get_response()
        self.wait_for_torrentlist_update()
        return response['result'] if response['result'] != 'success' else ''

    def increase_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if not self.torrent_details_cache['wanted'][num]:
                ref_num = num
                break
            if self.torrent_details_cache['priorities'][num] < \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if not self.torrent_details_cache['wanted'][ref_num]:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'high')

    def decrease_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if self.torrent_details_cache['priorities'][num] > \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if current_priority >= 1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'off')

    def set_file_priority(self, torrent_id, file_nums, priority):
        request_data = {'ids': [torrent_id]}
        if priority == 'off':
            request_data['files-unwanted'] = file_nums
        else:
            request_data['files-wanted'] = file_nums
            request_data['priority-' + priority] = file_nums
        request = TransmissionRequest(self.url, 'torrent-set', 1, request_data, server=self)
        request.send_request()
        self.wait_for_details_update()

    def get_file_priority(self, torrent_id, file_num):
        priority = self.torrent_details_cache['priorities'][file_num]
        if not self.torrent_details_cache['wanted'][file_num]:
            return 'off'
        if priority <= -1:
            return 'low'
        if priority == 0:
            return 'normal'
        if priority >= 1:
            return 'high'
        return '?'

    def wait_for_torrentlist_update(self):
        self.wait_for_update(7)

    def wait_for_details_update(self):
        self.wait_for_update(self.TAG_TORRENT_DETAILS)

    def wait_for_status_update(self):
        self.wait_for_update(22)

    def wait_for_update(self, update_id):
        self.update(0)  # send request
        while True:    # wait for response
            if self.update(0, update_id):
                break
            time.sleep(0.1)

    def get_status(self, torrent, narrow):
        if narrow:
            if torrent['status'] == Transmission.STATUS_STOPPED:
                status = 'P'
            elif torrent['status'] == Transmission.STATUS_CHECK:
                status = 'V'
            elif torrent['status'] == Transmission.STATUS_CHECK_WAIT:
                status = 'wV'
            elif torrent['isIsolated']:
                status = 'X'
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
                status = ('I', 'D')[torrent['rateDownload'] > 0]
                if torrent['metadataPercentComplete'] < 1:
                    status += 'M'
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD_WAIT:
                status = 'wD%d' % torrent['queuePosition']
            elif torrent['status'] == Transmission.STATUS_SEED:
                status = 'S'
            elif torrent['status'] == Transmission.STATUS_SEED_WAIT:
                status = 'wS%d' % torrent['queuePosition']
            else:
                status = '?'
        else:
            if torrent['status'] == Transmission.STATUS_STOPPED:
                status = 'paused'
            elif torrent['status'] == Transmission.STATUS_CHECK:
                status = 'verifying'
            elif torrent['status'] == Transmission.STATUS_CHECK_WAIT:
                status = 'will verify'
            elif torrent['isIsolated']:
                status = 'isolated'
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
                status = ('idle', 'downloading')[torrent['rateDownload'] > 0]
                if torrent['metadataPercentComplete'] < 1:
                    status += ' metadata'
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD_WAIT:
                status = 'will download (%d)' % torrent['queuePosition']
            elif torrent['status'] == Transmission.STATUS_SEED:
                status = 'seeding'
            elif torrent['status'] == Transmission.STATUS_SEED_WAIT:
                status = 'will seed (%d)' % torrent['queuePosition']
            else:
                status = 'unknown state'
        return status

    def can_has_peers(self, torrent):
        """ Will return True if at least one tracker was successfully queried
        recently, or if DHT is enabled for this torrent and globally, False
        otherwise. """

        # Torrent has trackers?
        if torrent['trackerStats']:
            # Did we try to connect a tracker?
            if any([tracker['hasAnnounced'] for tracker in torrent['trackerStats']]):
                for tracker in torrent['trackerStats']:
                    if tracker['lastAnnounceSucceeded']:
                        return True
            # We didn't try yet; assume at least one is online
            else:
                return True
        # Torrent can use DHT?
        # ('dht-enabled' may be missing; assume DHT is available until we can say for sure)
        return 'dht-enabled' not in self.status_cache or \
                (self.status_cache['dht-enabled'] and not torrent['isPrivate'])

    def get_bandwidth_priority(self, torrent):
        if torrent['bandwidthPriority'] == -1:
            return '-'
        if torrent['bandwidthPriority'] == 0:
            return ' '
        if torrent['bandwidthPriority'] == 1:
            return '+'
        return '?'

    def get_honors_session_limits(self, torrent):
        return ' ' if torrent['honorsSessionLimits'] else '*'

    def get_stats(self):
        request = TransmissionRequest(self.url, 'session-stats', 1, server=self)
        request.send_request()
        response = request.get_response()
        return response['arguments']

    def group_get(self):
        request = TransmissionRequest(self.url, 'group-get', self.TAG_GROUP_GET, server=self)
        request.send_request()
        response = request.get_response()
        if 'arguments' in response and 'group' in response['arguments']:
            return response['arguments']['group']
        return None

# End of Class Transmission


# User Interface
class Interface:
    TRACKER_ITEM_HEIGHT = 6

    def __init__(self, server):
        self.server = server
        if gconfig.profile in gconfig.profiles:
            self.apply_profile(gconfig.profiles[gconfig.profile])

        self.torrents = self.server.get_torrent_list(gconfig.sort_orders)
        self.stats = self.server.get_global_stats()
        self.torrent_details = []
        self.selected_torrent = -1  # changes to >-1 when focus >-1 & user hits return
        self.highlight_dialog = False
        self.search_focus = 0   # like self.focus but for searches in torrent list
        self.focused_id = -1  # the id (provided by Transmission) of self.torrents[self.focus]
        self.focus = -1  # -1: nothing focused; 0: top of list; <# of torrents>-1: bottom of list
        self.selected = set()
        self.scrollpos = 0   # start of torrentlist
        self.torrents_per_page = 0  # will be set by manage_layout()
        self.rateDownload_width = self.rateUpload_width = len(scale_bytes())
        self.rateDownload_width = self.get_rateDownload_width(self.torrents)
        self.rateUpload_width = self.get_rateUpload_width(self.torrents)

        self.details_category_focus = 0  # overview/files/peers/tracker in details
        self.focus_detaillist = -1  # same as focus but for details
        self.selected_files = []  # marked files in details
        self.file_index_map = {}  # Maps local torrent's file indices to server file indices
        self.scrollpos_detaillist = [0] * 5  # same as scrollpos but for details
        self.max_overview_scroll = 0
        self.exit_now = False
        self.vmode_id = -1
        self.filters_inverted = False
        self.force_narrow = None

        self.common_keybindings = {
            K.n0: self.action_profile_selected,
            K.n1: self.action_profile_selected,
            K.n2: self.action_profile_selected,
            K.n3: self.action_profile_selected,
            K.n4: self.action_profile_selected,
            K.n5: self.action_profile_selected,
            K.n6: self.action_profile_selected,
            K.n7: self.action_profile_selected,
            K.n8: self.action_profile_selected,
            K.n9: self.action_profile_selected,
            curses.KEY_SEND: lambda: self.move_queue('bottom'),
            curses.KEY_SHOME: lambda: self.move_queue('top'),
            curses.KEY_SLEFT: lambda: self.move_queue('ppage'),
            curses.KEY_SRIGHT: lambda: self.move_queue('npage'),
        }
        self.list_keybindings = {}
        self.details_keybindings = {}
        set_keys(gconfig.actions, self.common_keybindings, [0], self)
        set_keys(gconfig.actions, self.list_keybindings, [1], self)
        set_keys(gconfig.actions, self.details_keybindings, [2, 3, 4], self)

        self.filelist_needs_refresh = False
        self.sorted_files = None
        self.action_keys = {a:set(d[1]) for a, d in gconfig.actions.items()}
        parse_config_key(self, config, gconfig, self.common_keybindings, self.details_keybindings, self.list_keybindings, self.action_keys)

        try:
            self.init_screen()
            self.run()
        except curses.error:
            self.restore_screen()
            raise
        else:
            self.restore_screen()

    def apply_profile(self, profile):
        gconfig.sort_orders = [s.copy() for s in profile['sort']]
        # copy filter array from profile
        gconfig.filters = [[f.copy() for f in l] for l in profile['filter']]
        self.filters_inverted = False

    def save_profile(self, profile):
        gconfig.profiles[profile] = {'filter': [[f.copy() for f in l] for l in gconfig.filters],
                                     'sort': [s.copy() for s in gconfig.sort_orders]}

    def action_save_profile(self):
        name = self.dialog_input_text("Profile name to save:", "")
        if name:
            self.save_profile(name)

    def action_profile_selected(self, p):
        if p in range(K.n0, K.n9 + 1):
            p = chr(p)
        if p in gconfig.profiles:
            self.apply_profile(gconfig.profiles[p])

    def init_screen(self):
        os.environ['ESCDELAY'] = '0'  # make escape usable
        self.screen = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.screen.keypad(1)
        curses.halfdelay(10)  # STDIN timeout
        hide_cursor()
        gconfig.init_colors(dict(config.items('Colors')))

        # http://bugs.python.org/issue2675
        try:
            del os.environ['LINES']
            del os.environ['COLUMNS']
        except KeyError:
            pass

        signal.signal(signal.SIGWINCH, lambda y, frame: self.get_screen_size())
        self.get_screen_size()

    def restore_screen(self):
        curses.endwin()

    def get_screen_size(self):
        time.sleep(0.1)  # prevents curses.error on rapid resizing
        while True:
            curses.endwin()
            self.screen.refresh()
            self.height, self.width = self.screen.getmaxyx()
            # Tracker list breaks if width smaller than 73
            if not gconfig.PERMISSIVE and (self.width < 40 or self.height < 16):
                self.screen.erase()
                self.screen.addstr(0, 0, "Terminal too small", curses.A_BOLD)
                self.screen.addstr(1, 0, "Resize terminal or")
                self.screen.addstr(2, 0, "Press 'q' to  quit")
                c = self.screen.getch()
                if c in gconfig.esc_keys_w:
                    exit_prog()
            else:
                break
        self.manage_layout()
        # There are two extra lines here: One for a possible invisible line of
        # the last torrent, the other for avoiding 'last char of window bug'.
        self.pad = curses.newpad(self.height, self.width)

    def manage_layout(self):
        self.recalculate_torrents_per_page()
        self.detaillines_per_page = self.height - 8
        self.narrow = self.width < gconfig.narrow_threshold if self.force_narrow is None else self.force_narrow

        if self.selected_torrent > -1:
            self.rateDownload_width = self.get_rateDownload_width([self.torrent_details])
            self.rateUpload_width = self.get_rateUpload_width([self.torrent_details])
            self.torrent_title_width = self.width - self.rateUpload_width - 2
            # show downloading column only if torrents is downloading
            if self.torrent_details['status'] == Transmission.STATUS_DOWNLOAD:
                self.torrent_title_width -= self.rateDownload_width + 2

        elif self.torrents:
            self.visible_torrents_start = self.scrollpos // gconfig.tlist_item_height
            self.visible_torrents = self.torrents[self.visible_torrents_start: self.visible_torrents_start + self.torrents_per_page]
            self.rateDownload_width = self.get_rateDownload_width(self.visible_torrents)
            self.rateUpload_width = self.get_rateUpload_width(self.visible_torrents)
            self.torrent_title_width = self.width - self.rateUpload_width - 2
            # show downloading column only if any downloading torrents are visible
            if [x for x in self.visible_torrents if x['status'] == Transmission.STATUS_DOWNLOAD]:
                self.torrent_title_width -= self.rateDownload_width + 2
        else:
            self.visible_torrents = []
            self.torrent_title_width = 80

    def get_rateDownload_width(self, torrents):
        if torrents == []:
            return 4
        new_width = max([len(scale_bytes(x['rateDownload'])) for x in torrents])
        new_width = max(max([len(scale_time(x['eta'])) for x in torrents]), new_width)
        new_width = max(len(scale_bytes(self.stats['downloadSpeed'])), new_width)
        new_width = max(self.rateDownload_width, new_width)  # don't shrink
        return new_width

    def get_rateUpload_width(self, torrents):
        if torrents == []:
            return 4
        new_width = max([len(scale_bytes(x['rateUpload'])) for x in torrents] + [0])
        new_width = max(max([len(num2str(x['uploadRatio'], '%.02f')) for x in torrents] + [0]), new_width)
        new_width = max(len(scale_bytes(self.stats['uploadSpeed'])), new_width)
        new_width = max(self.rateUpload_width, new_width)  # don't shrink
        return new_width

    def recalculate_torrents_per_page(self):
        self.mainview_height = self.height - 2
        self.torrents_per_page = (self.mainview_height + gconfig.tlist_item_height - 1) // gconfig.tlist_item_height

    def run(self):
        self.draw_title_bar()
        self.draw_stats()
        self.draw_torrent_list()

        while True:
            self.server.update(1)

            if self.selected_torrent == -1:
                self.draw_torrent_list()
            else:
                self.draw_details()

            self.stats = self.server.get_global_stats()
            self.draw_title_bar()  # show shortcuts and stuff
            self.draw_stats()      # show global states
            self.screen.move(0, 0)  # in case cursor can't be invisible
            if self.handle_user_input() == -1:
                # No input for one second, so update file list.
                # It takes a long time, so avoid when handling user input
                if self.selected_torrent > -1 and self.details_category_focus == 1:
                    self.filelist_needs_refresh = True
                    self.server.set_torrent_details_id(self.torrents[self.focus]['id'])
                    self.server.wait_for_details_update()
            if self.exit_now:
                save_history(gconfig.history_file, gconfig.histories)
                return

    def action_daemon_quit(self):
        if self.dialog_yesno("Ask daemon to shut down?"):
            self.server.daemon_quit()

    def action_go_back_or_unfocus(self):
        if self.focus_detaillist > -1:   # unfocus and deselect file
            self.focus_detaillist = -1
            self.scrollpos_detaillist = [0] * 5
            self.selected_files = []
        else:  # return from details
            self.action_leave_details()

    def action_unfocus_torrent(self):
        if self.focus > -1:
            self.scrollpos = 0    # unfocus main list
            self.focus = -1
        elif gconfig.filters[0][0]['name']:
            gconfig.filters = [[{'name': '', 'inverse': False}]]  # reset filter

    def action_leave_details(self):
        self.server.set_torrent_details_id(-1)
        self.selected_torrent = -1
        self.details_category_focus = 0
        self.scrollpos_detaillist = [0] * 5
        self.selected_files = []
        self.vmode_id = -1

    def action_quit(self):
        self.exit_now = True

    def action_quit_now(self):
        self.exit_now = True

    def action_turtle_mode(self):
        self.server.toggle_turtle_mode()

    def action_move_queue_down(self):
        self.move_queue('down')

    def action_move_queue_up(self):
        self.move_queue('up')

    def action_add_torrent_paused(self):
        self.action_add_torrent(paused=True)

    def action_add_torrent(self, paused=False):
        free_space = None
        if self.server.get_rpc_version() >= 15:
            # 10^9 instead of 2^30 to be consistent with web interface
            free_space = float(self.server.get_free_space()) / (10**9)  # Bytes > GB
        pause = "(paused) " if paused else ""

        location = self.dialog_input_text("Add " + pause + "torrent from file, URL or pure hash"
                                          + (" - HDD (free): %.3f GB" % free_space if free_space else ""),
                                          homedir2tilde(os.getcwd() + os.sep), tab_complete='files')

        if location:
            if re.match('^[0-9a-fA-F]{40}$', location):
                location = 'magnet:?xt=urn:btih:{}'.format(location)

            error = self.server.add_torrent(tilde2homedir(location), paused=paused)
            if error:
                msg = wrap("Couldn't add torrent \"%s\":" % location)
                msg.extend(wrap(error, self.width - 4))
                self.dialog_ok("\n".join(msg))

    def action_enter_details(self):
        if self.focus > -1:
            self.screen.clear()
            self.selected_torrent = self.focus
            self.server.set_torrent_details_id(self.torrents[self.focus]['id'])
            self.server.wait_for_details_update()

    def action_show_torrent_sort_order_menu(self):
        if self.selected_torrent == -1:
            choice, inverse, _ = self.dialog_menu('Sort order', gconfig.sort_options,
                                      list(map(lambda x: x[0] == gconfig.sort_orders[-1]['name'], gconfig.sort_options)).index(True) + 1,
                                      extended=True)
            if choice != -128:
                if choice == 'reverse':
                    gconfig.sort_orders[-1]['reverse'] = not gconfig.sort_orders[-1]['reverse']
                else:
                    gconfig.sort_orders.append({'name': choice, 'reverse': inverse})
                    while len(gconfig.sort_orders) > 2:
                        gconfig.sort_orders.pop(0)

    def action_show_file_sort_order_menu(self):
        choice, inverse, _ = self.dialog_menu('Sort order', gconfig.file_sort_options, extended=True)
        if choice != -128:
            if choice != gconfig.file_sort_key:
                self.focus_detaillist = -1
                self.filelist_needs_refresh = True
            if choice == 'reverse':
                gconfig.file_sort_reverse = not gconfig.file_sort_reverse
            else:
                gconfig.file_sort_key = choice
                gconfig.file_sort_reverse = inverse

    def action_show_stats(self):
        title = "Global statistics"
        win = None
        while True:
            stats = self.server.get_stats()

            total_ul = stats['cumulative-stats']['uploadedBytes']
            total_dl = stats['cumulative-stats']['downloadedBytes']
            total_ratio = 'Inf' if not total_dl else round(float(total_ul) / float(total_dl), 2)
            total_time = stats['cumulative-stats']['secondsActive']

            session_ul = stats['current-stats']['uploadedBytes']
            session_dl = stats['current-stats']['downloadedBytes']
            session_ratio = 'Inf' if not session_dl else round(float(session_ul) / float(session_dl), 2)
            session_time = stats['current-stats']['secondsActive']

            message = ("CURRENT SESSION\n"
                       "  Uploaded:   {s_ul}\n"
                       "  Downloaded: {s_dl}\n"
                       "  Ratio:      {s_ratio}\n"
                       "  Duration:   {s_duration}\n\n"
                       "TOTAL\n"
                       "  Uploaded:   {t_ul}\n"
                       "  Downloaded: {t_dl}\n"
                       "  Ratio:      {t_ratio}\n"
                       "  Duration:   {t_duration}\n").format(s_ul=scale_bytes(session_ul),
                                                              s_dl=scale_bytes(session_dl),
                                                              s_ratio=session_ratio,
                                                              s_duration=scale_time(session_time, long=True),
                                                              t_ul=scale_bytes(total_ul),
                                                              t_dl=scale_bytes(total_dl),
                                                              t_ratio=total_ratio,
                                                              t_duration=scale_time(total_time, long=True))

            width = max([len(x) for x in message.split("\n")]) + 4
            width = min(self.width, width)
            height = min(self.height, message.count("\n") + 3)
            if win is None:
                win = self.window(height, width, message=message, title=title)
            else:
                self.win_message(win, height, width, message)
            key = self.wingetch(win)
            if key in gconfig.esc_keys_w:
                return -1
            self.update_torrent_list([win])

    def action_unmapped_actions(self):
        actions = []
        letters = 'abcdefghijklmnopqrstuvwxyz'
        i = 0
        accepted = [0, 1] if self.selected_torrent == -1 else [0, 2, 3, 4]
        for a in gconfig.actions:
            if not self.action_keys[a] and gconfig.actions[a][0] & 15 in accepted:
                actions.append((a, '_' + letters[i] + '. ' + gconfig.actions[a][2]))
                i += 1
        if actions == []:
            return
        c = self.dialog_menu('Choose action', actions, 0)
        if isinstance(c, str):
            f = getattr(self, 'action_' + c, lambda: None)
            f()

    def choose_profile(self):
        profiles = []
        keys = set({})
        for p in gconfig.profiles:
            if p[0] in keys:
                profiles.append((p, p))
            else:
                keys.add(p[0])
                profiles.append((p, '_' + p))
        c = self.dialog_menu('Choose profile', profiles, 0)
        if c in gconfig.profiles:
            self.apply_profile(gconfig.profiles[c])

    def filter_menu(self, oldfilter={'name': '', 'inverse': False}, prompt="", winstack=[]):
        new_filter = oldfilter.copy()
        options = [('uploading', '_Uploading'), ('downloading', '_Downloading'),
                   ('active', 'Ac_tive'), ('paused', '_Paused'), ('seeding', '_Seeding'),
                   ('incomplete', 'In_complete'), ('verifying', 'Verif_ying'),
                   ('private', 'P_rivate'), ('isolated', '_Isolated'),
                   ('tracker', 'Trac_ker'),
                   ('regex', 'Regular e_xpression'),
                   ('location', 'L_ocation'),
                   ('selected', 'S_elected'),
                   ('honors', '_Honors limits'),
                   ('partwanted', 'Part _wanted'),
                   ('error', 'Error/Warnin_g'),
                   ('invert', 'In_vert'), ('', '_All')]
        if self.server.get_rpc_version() >= 16:
            options.insert(-2, ('label', 'La_bel'))
        if self.server.get_rpc_version() >= 16:
            options.insert(-2, ('group', 'Ba_ndwidth group'))
        try:
            s = list(map(lambda x: x[0] == oldfilter['name'], options)).index(True) + 1
        except Exception:
            s = 0
        choice, inverse, win = self.dialog_menu(prompt, options, s, extended=True, winstack=winstack)
        if choice != -128:
            if choice == 'invert':
                new_filter['inverse'] = not new_filter['inverse']
            else:
                if choice in ['tracker', 'location', 'label', 'group']:
                    if choice == 'tracker':
                        select = sorted(self.server.trackers)
                        min_select = 2
                    elif choice == 'location':
                        select = sorted(self.server.locations)
                        min_select = 2
                    elif choice == 'label':
                        select = sorted(self.server.labels)
                        min_select = 1
                    elif choice == 'group':
                        select = sorted(self.server.groups)
                        min_select = 1
                    current_choice = new_filter[choice] if choice in new_filter else ''
                    if len(select) < min_select:
                        # Nothing to select
                        return None
                    indexes = 'abcdefghijklmnopqrstuvwxyz1234567890'
                    select_list = []
                    i = 0
                    for x in select:
                        if i < len(indexes):
                            select_list.append((x, '_' + indexes[i] + '. ' + x))
                            i = i + 1
                        else:
                            select_list.append((x, '   ' + x))
                    try:
                        s = list(map(lambda x: x[0] == current_choice, select_list)).index(True) + 1
                    except Exception:
                        s = 0
                    selected = self.dialog_menu('Select ' + choice, select_list, s, winstack=winstack + [win])
                    if selected not in select:
                        return None
                    new_filter[choice] = selected
                elif choice == 'regex':
                    regex = self.dialog_input_text('Regular expression to filter (case insensitive):',
                                                   new_filter['regex'] if 'regex' in new_filter else '', winstack=winstack + [win])
                    if regex == '':
                        return None
                    new_filter['regex'] = regex
                new_filter['name'] = choice
                new_filter['inverse'] = inverse
            return new_filter
        return None

    def action_set_filter(self):
        prompt = ('Show only', 'Filter all')[gconfig.filters[0][0]['inverse']]
        new_filter = self.filter_menu(gconfig.filters[0][0], prompt=prompt)
        if new_filter:
            gconfig.filters = [[new_filter]]
            self.filters_inverted = False

    def action_invert_filters(self):
        self.filters_inverted = not self.filters_inverted

    def action_add_filter_line(self):
        new_filter = self.filter_menu(prompt="Add filter:")
        if new_filter:
            gconfig.filters.append([new_filter])

    def action_add_filter(self):
        new_filter = self.filter_menu(prompt="Add filter:")
        if new_filter:
            gconfig.filters[0].append(new_filter)

    def action_edit_filters(self):
        gconfig.filters = self.dialog_filters()

    def action_global_upload(self):
        current_limit = (-1, self.stats['speed-limit-up'])[self.stats['speed-limit-up-enabled']]
        limit = self.dialog_input_number("Global upload limit in kilobytes per second", current_limit)
        if limit == -128:
            return
        self.server.set_rate_limit('up', limit)

    def action_global_download(self):
        current_limit = (-1, self.stats['speed-limit-down'])[self.stats['speed-limit-down-enabled']]
        limit = self.dialog_input_number("Global download limit in kilobytes per second", current_limit)
        if limit == -128:
            return
        self.server.set_rate_limit('down', limit)

    def action_group_upload(self):
        self.group_set_limit('up')

    def action_group_download(self):
        self.group_set_limit('down')

    def action_group_get(self):
        gs = self.server.group_get()
        if gs:
            namewidth = max((len(g['name']) for g in gs))
            groups_str = "name".rjust(4 + namewidth) + ":      down,        up ignores session\n\n"
            for g in gs:
                groups_str += (g['name'].rjust(4 + namewidth) + ": " +
                    ("{:9}, ".format(g['downloadLimit']) if g['downloadLimited'] else "unlimited, ") +
                    ("{:9}".format(g['uploadLimit']) if g['uploadLimited'] else "unlimited") +
                    ("\n" if g['honorsSessionLimits'] else " *\n"))
            self.dialog_ok(groups_str)


    def group_set_limit(self, direction):
        group = ''
        if self.selected_torrent > -1:
            group = self.torrent_details['group']
        elif self.focus > -1:
            group = self.torrents[self.focus]['group']
        if not group:
            return
        current_limit = (-1, self.stats['speed-limit-'+direction])[self.stats['speed-limit-'+direction+'-enabled']]
        limit = self.dialog_input_number(direction.title()+'load limit in kilobytes per second for group '+group, current_limit)
        if limit == -128:
            return
        self.server.set_rate_limit(direction, limit, group=group)

    def selected_ids(self):
        # If viewing torrent details, act on viewed torrent, even if there is a
        # selection.
        if self.selected_torrent > -1:
            return [self.torrent_details['id']]
        if self.selected:
            return list(self.selected)
        if self.focus == -1:
            return []
        return [self.torrents[self.focus]['id']]

    # Decide which torrent name to show for confirmation/prompt:
    # If focused torret is in the selection - select it.
    # Otherwise, the first selected torrent.
    # Also calculate the extra line: "and %d more", if more than one
    # torrent is selected.
    def get_focused(self, ids):
        if ids == []:
            return (None, "")
        focused = self.torrents[self.focus] if (self.focus > -1 and self.torrents[self.focus]['id'] in ids) \
            else self.server.get_torrent_by_id(ids[0])
        extraline = "\nand %d more" % (len(ids) - 1) if len(ids) > 1 else ""
        return (focused, extraline)

    def torrent_up_down_load(self, direction):
        ids = self.selected_ids()
        if ids and direction in ['up', 'down']:
            focused, extraline = self.get_focused(ids)
            current_limit = (-1, focused[direction + 'loadLimit'])[focused[direction + 'loadLimited']]
            limit = self.dialog_input_number(direction.capitalize() + "load limit in kilobytes per second for\n%s" % focused['name'] +
                                             extraline, current_limit)
            if limit == -128:
                return
            self.server.set_rate_limit(direction, limit, ids)

    def action_torrent_upload(self):
        self.torrent_up_down_load('up')

    def action_torrent_download(self):
        self.torrent_up_down_load('down')

    def action_seed_ratio(self):
        ids = self.selected_ids()
        if ids:
            focused = self.torrents[self.focus] if (self.focus > -1 and self.torrents[self.focus]['id'] in ids) \
                else self.server.get_torrent_by_id(ids[0])
            if focused['seedRatioMode'] == 0:   # Use global settings
                current_limit = ''
            elif focused['seedRatioMode'] == 1:  # Stop seeding at seedRatioLimit
                current_limit = focused['seedRatioLimit']
            elif focused['seedRatioMode'] == 2:  # Seed regardless of ratio
                current_limit = -1
            limit = self.dialog_input_number("Seed ratio limit for\n%s" % focused['name'] +
                                             ("\nand %d more" % (len(ids) - 1) if len(ids) > 1 else ""),
                                             current_limit, floating_point=True, allow_empty=True)
            if limit == -1:
                limit = 0
            if limit == -2:  # -2 means 'empty' in dialog_input_number return codes
                limit = -1
            self.server.set_seed_ratio(float(limit), ids)

    def action_honors_limits(self):
        self.server.toggle_honors_session_limits(self.selected_ids())

    def action_bandwidth_priority_dec(self):
        self.server.decrease_bandwidth_priority(self.selected_ids())

    def action_bandwidth_priority_inc(self):
        self.server.increase_bandwidth_priority(self.selected_ids())

    def action_copy_magnet_link(self):
        if self.focus > -1:
            magnet = self.torrents[self.focus]['magnetLink']
            try:
                pyperclip.copy(magnet)
            except Exception as e:
                self.dialog_ok(str(e))

    def move_queue(self, direction):
        # queue was implemmented in Transmission v2.4
        if self.server.get_rpc_version() >= 14 and self.focus > -1:
            if direction in ('ppage', 'npage'):
                new_position = self.torrents[self.focus]['queuePosition']
                if direction == 'ppage':
                    new_position -= 10
                else:
                    new_position += 10
            else:
                new_position = direction
            self.server.move_queue(self.torrents[self.focus]['id'], new_position)

    def action_pause_unpause_torrent(self):
        ids = self.selected_ids()
        if ids:
            if any(self.server.get_torrent_by_id(i)['status'] == Transmission.STATUS_STOPPED for i in ids):
                self.server.start_torrents(ids)
            else:
                self.server.stop_torrents(ids)

    def action_start_now_torrent(self):
        ids = self.selected_ids()
        if ids:
            self.server.start_now_torrent(ids)

    def action_pause_unpause_all_torrent(self):
        if len(self.torrents) > 0:
            focused_torrent = self.torrents[max(0, self.focus)]
            if focused_torrent['status'] == Transmission.STATUS_STOPPED:
                self.server.start_torrents([t['id'] for t in self.torrents])
            else:
                self.server.stop_torrents([t['id'] for t in self.torrents])

    def action_verify_torrent(self):
        ids = self.selected_ids()
        ids = [i for i in ids if self.server.get_torrent_by_id(i)['status'] not in
               [Transmission.STATUS_CHECK, Transmission.STATUS_CHECK_WAIT]]
        if ids:
            self.server.verify_torrent(ids)

    def action_reannounce_torrent(self):
        ids = self.selected_ids()
        if ids:
            self.server.reannounce_torrent(ids)


    def conditional_remove(self, ids, first, data=False):
        if ids:
            hard="WARNING: this will remove more than one torrent" if len(ids)>1 else None
            if first in ids:
                ids.remove(first)
            else:
                first=ids.pop(0)
            name = self.server.get_torrent_by_id(first)['name'][:self.width - 20]
            if ids:
                extraline = " And:\n"
                for i in ids[:self.height - 12]:
                    extraline = extraline + " " + self.server.get_torrent_by_id(i)['name'][:self.width - 8] + "\n"
                if len(ids)> self.height - 12:
                    extraline += "   and even %d more." % (len(ids)-self.height - 12)
            else:
                extraline = "\n"
            ids.append(first)
            question = "Remove AND DELETE" if data else "Remove"
            if self.dialog_yesno(question + " %s?" % name + extraline, hard=hard, important=data):
                self.server.remove_torrent(ids, data=data)
                if self.selected_torrent > -1:
                    self.action_leave_details()
                self.focus_next_after_delete()

    def action_remove(self):
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            self.conditional_remove(ids, focused['id'])

    def action_remove_selected(self):
        if not self.selected:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            self.conditional_remove(ids, focused['id'])

    def action_remove_focused(self):
        if self.focus > -1:
            ids = [self.torrents[self.focus]['id']]
            self.conditional_remove(ids, ids[0])

    def action_remove_data(self):
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            self.conditional_remove(ids, focused['id'], data=True)

    def action_remove_selected_data(self):
        if not self.selected:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            self.conditional_remove(ids, focused['id'], data=True)

    def action_remove_focused_data(self):
        if self.focus > -1:
            ids = [self.torrents[self.focus]['id']]
            self.conditional_remove(ids, ids[0], data=True)

    def focus_next_after_delete(self):
        """ Focus next torrent after user deletes torrent
            self.torrents still includes the deleted torrent
        """

        new_focus = min(self.focus + 1, len(self.torrents) - 2)
        if new_focus != self.focus:
            self.focused_id = self.torrents[new_focus]['id']
        else:
            self.focused_id = self.torrents[new_focus + 1]['id']

    def add_tracker(self):
        if self.server.get_rpc_version() < 10:
            self.dialog_ok("You need Transmission v2.10 or higher to add trackers.")
            return

        tracker = self.dialog_input_text('Add tracker URL:', history=gconfig.histories['tracker'], history_max=10,
                                         fixed_history=list(self.server.trackers))
        if tracker:
            t = self.torrent_details
            response = self.server.add_torrent_tracker(t['id'], tracker)

            if response:
                msg = wrap("Couldn't add tracker: %s" % response)
                self.dialog_ok("\n".join(msg))

    def action_remove_tracker(self):
        if self.details_category_focus == 3:
            if self.server.get_rpc_version() < 10:
                self.dialog_ok("You need Transmission v2.10 or higher to remove trackers.")
                return

            t = self.torrent_details
            if (self.scrollpos_detaillist[3] >= 0
                    and self.scrollpos_detaillist[3] < len(t['trackerStats'])
                    and self.dialog_yesno("Do you want to remove this tracker?")):

                tracker = t['trackerStats'][self.scrollpos_detaillist[3]]
                response = self.server.remove_torrent_tracker(t['id'], tracker['id'])

                if response:
                    msg = wrap("Couldn't remove tracker: %s" % response)
                    self.dialog_ok("\n".join(msg))

    def action_page_up(self):
        self.movement_keys('page_up')

    def action_page_down(self):
        self.movement_keys('page_down')

    def action_line_up(self):
        self.movement_keys('line_up')

    def action_line_down(self):
        self.movement_keys('line_down')

    def action_go_home(self):
        self.movement_keys('home')

    def action_go_end(self):
        self.movement_keys('end')

    def movement_keys(self, action):
        if self.selected_torrent == -1 and len(self.torrents) > 0:
            if action == 'line_up':
                self.focus, self.scrollpos = self.move_up(self.focus, self.scrollpos, gconfig.tlist_item_height)
            elif action == 'line_down':
                self.focus, self.scrollpos = self.move_down(self.focus, self.scrollpos, gconfig.tlist_item_height,
                                                            self.torrents_per_page, len(self.torrents))
            elif action == 'page_up':
                self.focus, self.scrollpos = self.move_page_up(self.focus, self.scrollpos, gconfig.tlist_item_height,
                                                               self.torrents_per_page)
            elif action == 'page_down':
                self.focus, self.scrollpos = self.move_page_down(self.focus, self.scrollpos, gconfig.tlist_item_height,
                                                                 self.torrents_per_page, len(self.torrents))
            elif action == 'home':
                self.focus, self.scrollpos = self.move_to_top()
            elif action == 'end':
                self.focus, self.scrollpos = self.move_to_end(gconfig.tlist_item_height, self.torrents_per_page, len(self.torrents))
            self.focused_id = self.torrents[self.focus]['id']
        elif self.selected_torrent > -1:
            # overview
            if self.details_category_focus == 0:
                if action == 'line_up' and self.scrollpos_detaillist[0] > 0:
                    self.scrollpos_detaillist[0] -= 1
                elif action == 'line_down' and self.scrollpos_detaillist[0] < self.max_overview_scroll:
                    self.scrollpos_detaillist[0] += 1
                elif action == 'home':
                    self.scrollpos_detaillist[0] = 0
                elif action == 'end':
                    self.scrollpos_detaillist[0] = self.max_overview_scroll
            # file list
            if self.details_category_focus == 1:
                # focus/movement
                if action == 'line_up':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = \
                        self.move_up(self.focus_detaillist, self.scrollpos_detaillist[1], 1)
                elif action == 'line_down':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = \
                        self.move_down(self.focus_detaillist, self.scrollpos_detaillist[1], 1,
                                       self.detaillines_per_page, len(self.torrent_details['files']))
                elif action == 'page_up':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = \
                        self.move_page_up(self.focus_detaillist, self.scrollpos_detaillist[1], 1,
                                          self.detaillines_per_page)
                elif action == 'page_down':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = \
                        self.move_page_down(self.focus_detaillist, self.scrollpos_detaillist[1], 1,
                                            self.detaillines_per_page, len(self.torrent_details['files']))
                elif action == 'home':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = self.move_to_top()
                elif action == 'end':
                    self.focus_detaillist, self.scrollpos_detaillist[1] = \
                        self.move_to_end(1, self.detaillines_per_page, len(self.torrent_details['files']))
                # visual mode
                if self.vmode_id > -1:
                    if self.vmode_id < self.focus_detaillist:
                        self.selected_files = list(range(self.vmode_id, self.focus_detaillist + 1))
                    else:
                        self.selected_files = list(range(self.focus_detaillist, self.vmode_id + 1))
            list_len = 0
            ppage = 1

            # peer list movement
            if self.details_category_focus == 2:
                list_len = len(self.torrent_details['peers'])
                lines_per_page = self.detaillines_per_page

            # tracker list movement
            elif self.details_category_focus == 3:
                list_len = len(self.torrent_details['trackerStats'])
                lines_per_page = max(1, self.detaillines_per_page // (self.TRACKER_ITEM_HEIGHT + 2))
                ppage = 0

            # pieces list movement
            elif self.details_category_focus == 4:
                piece_count = self.torrent_details['pieceCount']
                margin = len(str(piece_count)) + 2
                map_width = int(str(self.width - margin - 1)[0:-1] + '0')
                list_len = (piece_count // map_width) + 1
                lines_per_page = self.detaillines_per_page

            if list_len:
                if action == 'line_up':
                    if self.scrollpos_detaillist[self.details_category_focus] > 0:
                        self.scrollpos_detaillist[self.details_category_focus] -= 1
                elif action == 'line_down':
                    if self.scrollpos_detaillist[self.details_category_focus] < list_len - 1:
                        self.scrollpos_detaillist[self.details_category_focus] += 1
                elif action == 'page_up':
                    self.scrollpos_detaillist[self.details_category_focus] = \
                        max(self.scrollpos_detaillist[self.details_category_focus] - lines_per_page - ppage, 0)
                elif action == 'page_down':
                    self.scrollpos_detaillist[self.details_category_focus] = min(list_len - 1,
                                                                                 self.scrollpos_detaillist[self.details_category_focus] + lines_per_page)
                elif action == 'home':
                    self.scrollpos_detaillist[self.details_category_focus] = 0
                elif action == 'end':
                    self.scrollpos_detaillist[self.details_category_focus] = list_len - 1

            # Disallow scrolling past the last item that would cause blank
            # space to be displayed in pieces and peer lists.
            if self.details_category_focus in (2, 4):
                self.scrollpos_detaillist[self.details_category_focus] = min(self.scrollpos_detaillist[self.details_category_focus],
                                                                             max(0, list_len - self.detaillines_per_page))

    def action_file_priority_or_switch_details_next(self):
        if self.details_category_focus == 1 and \
                (self.selected_files or self.focus_detaillist > -1):
            if self.selected_files:
                files = {self.file_index_map[index] for index in self.selected_files}
                self.server.increase_file_priority(files)
            elif self.focus_detaillist > -1:
                self.server.increase_file_priority([self.file_index_map[self.focus_detaillist]])
            self.filelist_needs_refresh = True
        else:
            self.action_next_details()

    def action_file_priority_or_switch_details_prev(self):
        if self.details_category_focus == 1 and \
                (self.selected_files or self.focus_detaillist > -1):
            if self.selected_files:
                files = {self.file_index_map[index] for index in self.selected_files}
                self.server.decrease_file_priority(files)
            elif self.focus_detaillist > -1:
                self.server.decrease_file_priority([self.file_index_map[self.focus_detaillist]])
            self.filelist_needs_refresh = True
        else:
            self.action_prev_details()

    def action_rename_dir(self):
        self.rename_torrent_selected_file(True)

    def action_rename_torrent_selected_file(self):
        self.rename_torrent_selected_file()

    def rename_torrent_selected_file(self, rename_dir=False):
        def rename_dialog(oldname):
            filename = os.path.basename(oldname)
            msg = 'Rename "%s"\nto:' % oldname
            newname = self.dialog_input_text(msg, filename, tab_complete='dirs')
            if newname:
                if len(newname.split(os.sep)) > 1:
                    self.dialog_ok("Moving is not supported.")
                else:
                    result = self.server.rename_torrent_file(self.torrents[self.focus]['id'], oldname, newname)
                    if result == 'success':
                        return os.path.join(os.path.dirname(oldname), newname)
                    self.dialog_ok('Couldn\'t rename\n"%s"\nto\n"%s":\n%s' %
                                   (oldname, newname, result))
                    return None
            return None

        if self.selected_torrent > -1 and self.details_category_focus == 1 and self.focus_detaillist >= 0:
            # rename files in torrent
            file_id = self.file_index_map[self.focus_detaillist]
            name = self.torrent_details['files'][file_id]['name']
            if rename_dir:
                name = os.path.dirname(name)
                if not os.sep in name:
                    # Don't rename torrent
                    return
            newpath = rename_dialog(name)
            if newpath:
                if not rename_dir:
                    # This shows new name immediately, but for dirs it is
                    # simpler to wait for the new name from the server
                    self.torrent_details['files'][file_id]['name'] = newpath
                self.filelist_needs_refresh = True  # force read
        elif self.focus > -1:
            # rename torrent folder
            rename_dialog(self.torrents[self.focus]['name'])

    def action_add_tracker_or_select_all_files(self):
        # File list
        if self.details_category_focus == 1:
            self.select_unselect_file('all')
        # Trackers
        elif self.details_category_focus == 3:
            self.add_tracker()

    def action_visual_select_files(self):
        self.select_unselect_file('visual')

    def action_invert_selection_files(self):
        self.select_unselect_file('invert')

    def action_select_file(self):
        self.select_unselect_file('file')

    def action_select_files_dir(self):
        self.select_unselect_file('dir')

    def select_unselect_file(self, action):
        if self.details_category_focus == 1 and self.focus_detaillist >= 0:
            # file selection with space
            if action == 'file':
                try:
                    self.selected_files.pop(self.selected_files.index(self.focus_detaillist))
                except ValueError:
                    self.selected_files.append(self.focus_detaillist)
                self.action_line_down()
            # (un)select directory
            elif action == 'dir':
                file_id = self.file_index_map[self.focus_detaillist]
                focused_dir = os.path.dirname(self.torrent_details['files'][file_id]['name'])
                if self.selected_files.count(self.focus_detaillist):
                    for focus in range(0, len(self.torrent_details['files'])):
                        file_id = self.file_index_map[focus]
                        if self.torrent_details['files'][file_id]['name'].startswith(focused_dir):
                            try:
                                while focus in self.selected_files:
                                    self.selected_files.remove(focus)
                            except ValueError:
                                pass
                else:
                    for focus in range(0, len(self.torrent_details['files'])):
                        file_id = self.file_index_map[focus]
                        if self.torrent_details['files'][file_id]['name'].startswith(focused_dir):
                            self.selected_files.append(focus)
                self.action_move_to_next_directory()
            # (un)select all files
            elif action == 'all':
                if self.selected_files:
                    self.selected_files = []
                else:
                    self.selected_files = list(range(0, len(self.torrent_details['files'])))
            elif action == 'invert':
                self.selected_files = [f for f in range(0, len(self.torrent_details['files'])) if f not in self.selected_files]
            elif action == 'visual':
                if self.selected_files:
                    self.selected_files = []
                if self.vmode_id != -1:
                    self.vmode_id = -1
                else:
                    try:
                        self.selected_files.pop(self.selected_files.index(self.focus_detaillist))
                    except ValueError:
                        self.selected_files.append(self.focus_detaillist)
                    self.vmode_id = self.focus_detaillist

    def action_move_to_next_directory(self):
        if self.details_category_focus == 1:
            self.focus_detaillist = max(self.focus_detaillist, 0)
            file_id = self.file_index_map[self.focus_detaillist]
            focused_dir = os.path.dirname(self.torrent_details['files'][file_id]['name'])
            while self.torrent_details['files'][file_id]['name'].startswith(focused_dir) \
                    and self.focus_detaillist < len(self.torrent_details['files']) - 1:
                self.action_line_down()
                file_id = self.file_index_map[self.focus_detaillist]

    def action_move_to_previous_directory(self):
        if self.details_category_focus == 1:
            self.focus_detaillist = max(self.focus_detaillist, 0)
            file_id = self.file_index_map[self.focus_detaillist]
            focused_dir = os.path.dirname(self.torrent_details['files'][file_id]['name'])
            while self.torrent_details['files'][file_id]['name'].startswith(focused_dir) \
                    and self.focus_detaillist > 0:
                self.action_line_up()
                file_id = self.file_index_map[self.focus_detaillist]

    def action_file_info(self):
        if self.details_category_focus == 1 and self.focus_detaillist > -1:
            file_id = self.file_index_map[self.focus_detaillist]
            name = self.torrent_details['files'][file_id]['name']
            if '/' in name:
                name = '/'.join(name.split('/')[1:])
            size = str(self.torrent_details['files'][file_id]['length'])
            have = str(self.torrent_details['files'][file_id]['bytesCompleted']).rjust(len(size))
            msg = "%s\nSize: %s\nHave: %s" % (name, size, have)
            width = max(len(name), len(size), 15)+10
            win = self.window(6, width, msg)
            while True:
                key = self.wingetch(win)
                if key in gconfig.esc_keys_w:
                    return -1

    def action_view_file(self):
        self.view_file(gconfig.file_viewer, gconfig.file_open_in_terminal)

    def view_file(self, file_viewer, file_open_in_terminal):
        if self.details_category_focus == 1:
            details = self.server.get_torrent_details()
            stats = self.server.get_global_stats()

            if gconfig.view_selected and self.selected_files:
                files = [self.file_index_map[f] for f in self.selected_files]
            elif self.focus_detaillist >= 0:
                files = [self.file_index_map[self.focus_detaillist]]
            else:
                return
            file_names = []
            for file_server_index in files:
                file_name = details['files'][file_server_index]['name']

                download_dir = details['downloadDir']
                incomplete_dir = stats['incomplete-dir'] + '/'

                file_path = None
                possible_file_locations = [
                    download_dir + file_name,
                    download_dir + file_name + '.part',
                    incomplete_dir + file_name,
                    incomplete_dir + file_name + '.part'
                ]

                for f in possible_file_locations:
                    if os.path.isfile(f):
                        file_path = f
                        break
                if file_path:
                    file_names.append(file_path)

            if not file_names:
                self.dialog_ok("Could not find file:\n%s" % (file_name))
                return

            viewer_cmd = []
            for argstr in file_viewer.split(" "):
                if argstr == '%s':
                    viewer_cmd.extend(file_names)
                else:
                    viewer_cmd.append(argstr)
            try:
                if file_open_in_terminal:
                    self.restore_screen()
                    call(viewer_cmd)
                    self.get_screen_size()
                else:
                    devnull = open(os.devnull, 'wb')
                    Popen(viewer_cmd, stdout=devnull, stderr=devnull)
                    devnull.close()
            except OSError as err:
                self.get_screen_size()
                self.dialog_ok("%s:\n%s" % (" ".join(viewer_cmd), err))
            hide_cursor()
            if gconfig.file_viewer != file_viewer:
                file_type = self.server.get_torrent_details()['files'][self.file_index_map[self.focus_detaillist]]['name'].split('.')[-1]
                gconfig.histories['types'][file_type] = file_viewer

    def action_view_file_command(self):
        file_type = self.server.get_torrent_details()['files'][self.file_index_map[self.focus_detaillist]]['name'].split('.')[-1]
        if file_type in gconfig.histories['types']:
            command = gconfig.histories['types'][file_type]
        else:
            command = ''
        self.dialog_input_text('Command to run (%s will be replaced by file name)', command,
                               tab_complete='executable',
                               on_enter=self.view_file_command,
                               history=gconfig.histories['command'], history_max=10,
                               fixed_history=[gconfig.file_viewer])

    def view_file_command(self, pattern, inc=1, search=None):
        self.view_file(pattern, gconfig.file_open_in_terminal if inc == 1 else not inc)
        return True

    def action_tab_files(self):
        self.filelist_needs_refresh = True
        self.details_category_focus = 1

    def action_tab_overview(self):
        self.details_category_focus = 0

    def action_tab_peers(self):
        self.details_category_focus = 2

    def action_tab_trackers(self):
        self.details_category_focus = 3

    def action_tab_chunks(self):
        self.details_category_focus = 4

    def action_profile_menu(self):
        if len(gconfig.profiles) >= 1:
            self.choose_profile()

    def action_remove_labels(self):
        if self.server.get_rpc_version() < 16:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            name = focused['name'][:self.width - 15]
            if self.dialog_yesno("Remove labels from %s?" % name + extraline):
                self.server.set_labels(ids, [])

    def action_set_group(self):
        if self.server.get_rpc_version() < 16:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            msg = ('Set group of "%s"' % focused['name']) + extraline + '\nto:'
            group = self.dialog_input_text(msg, '')
            if group:
                self.server.set_group(ids, group)

    def action_set_labels(self):
        if self.server.get_rpc_version() < 16:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            msg = ('Set labels of "%s"' % focused['name']) + extraline + '\nto:'
            labels_str = self.dialog_input_text(msg, '')
            labels = [s.strip() for s in labels_str.split(',')]
            if labels:
                self.server.set_labels(ids, labels)

    def action_add_label(self):
        if self.server.get_rpc_version() < 16:
            return
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            msg = ('Label to add to "%s"' % focused['name']) + extraline
            label = self.dialog_input_text(msg, '', history=gconfig.histories['label'], history_max=10, fixed_history=list(self.server.labels))
            if label:
                self.server.add_label(ids, label)

    def action_toggle_compact_torrentlist(self):
        gconfig.tlist_item_height = gconfig.tlist_item_height % 3 + 1
        self.recalculate_torrents_per_page()
        self.follow_list_focus()

    def action_toggle_torrent_numbers(self):
        gconfig.torrent_numbers = not gconfig.torrent_numbers

    def action_move_torrent(self):
        ids = self.selected_ids()
        if ids:
            focused, extraline = self.get_focused(ids)
            location = homedir2tilde(self.torrents[self.focus]['downloadDir'])
            msg = ('Move "%s"' % focused['name']) + extraline + '\nfrom %s to' % location
            path = self.dialog_input_text(msg, location, tab_complete='dirs',
                                          history=gconfig.histories['location'], history_max=10, fixed_history=list(self.server.locations))
            if path:
                self.server.move_torrent(ids, tilde2homedir(path))

    def handle_user_input(self):
        c = self.screen.getch()
        if c == -1:
            return -1
        if c in self.common_keybindings:
            f = self.common_keybindings[c]
        elif self.selected_torrent == -1:
            f = self.list_keybindings.get(c, None)
        else:
            f = self.details_keybindings.get(c, None)
        if f:
            #Temporarily:
            if f == self.action_profile_selected:
                f(c)
            else:
                f()
        try:
            if self.selected_torrent == -1:
                self.draw_torrent_list()
            else:
                self.draw_details()
        except Exception as e:
            pdebug('caught %s in handle_user_input(): %s\n' % (type(e), str(e)))
        return c

    def action_invert_selection_torrents(self):
        if self.selected_torrent == -1:
            self.action_select_unselect_torrent(invert=True)

    def action_select_unselect_torrents(self):
        if self.selected_torrent == -1:
            self.action_select_unselect_torrent(all_torrents=True)

    def action_select_unselect_torrent(self, all_torrents=False, invert=False):
        if all_torrents:
            if self.selected:
                self.selected = set()
            else:
                self.selected = {x['id'] for x in self.torrents}
        elif invert:
            self.selected.symmetric_difference_update({x['id'] for x in self.torrents})
        else:
            if self.focus != -1:
                self.selected.symmetric_difference_update(set([self.torrents[self.focus]['id']]))
                self.action_line_down()

    def filter_torrent(self, t, filtr):
        if filtr['name'] == 'downloading':
            return filtr['inverse'] != (t['rateDownload'] > 0)
        if filtr['name'] == 'uploading':
            return filtr['inverse'] != (t['rateUpload'] > 0)
        if filtr['name'] == 'paused':
            return filtr['inverse'] != (t['status'] == Transmission.STATUS_STOPPED)
        if filtr['name'] == 'seeding':
            return filtr['inverse'] != (t['status'] == Transmission.STATUS_SEED or t['status'] == Transmission.STATUS_SEED_WAIT)
        if filtr['name'] == 'incomplete':
            return filtr['inverse'] != (t['percentDone'] < 100)
        if filtr['name'] == 'private':
            return filtr['inverse'] != t['isPrivate']
        if filtr['name'] == 'active':
            return filtr['inverse'] != (t['peersGettingFromUs'] > 0 or t['peersSendingToUs'] > 0 or t['status'] == Transmission.STATUS_CHECK)
        if filtr['name'] == 'verifying':
            return filtr['inverse'] != (t['status'] == Transmission.STATUS_CHECK or t['status'] == Transmission.STATUS_CHECK_WAIT)
        if filtr['name'] == 'isolated':
            return filtr['inverse'] != t['isIsolated']
        if filtr['name'] == 'honors':
            return filtr['inverse'] != t['honorsSessionLimits']
        if filtr['name'] == 'selected':
            return filtr['inverse'] != (t['id'] in self.selected)
        if filtr['name'] == 'tracker':
            return filtr['inverse'] != (t['mainTrackerDomain'] == filtr['tracker'])
        if filtr['name'] == 'regex':
            return filtr['inverse'] != bool(re.search(filtr['regex'], t['name'], flags=re.I))
        if filtr['name'] == 'location':
            return filtr['inverse'] != (homedir2tilde(t['downloadDir']) == filtr['location'])
        if filtr['name'] == 'label':
            return filtr['inverse'] != (filtr['label'] in t['labels'])
        if filtr['name'] == 'group':
            return filtr['inverse'] != (filtr['group'] == t['group'])
        if filtr['name'] == 'partwanted':
            return filtr['inverse'] != (t['totalSize'] > t['sizeWhenDone'])
        if filtr['name'] == 'error':
            return filtr['inverse'] != (t['error'] > 0)

        return True  # Unknown filter does not filter anything

    def filter_torrent_list(self):
        self.torrents = [t for t in self.torrents if any(all(self.filter_torrent(t, f) for f in fs) for fs in gconfig.filters) != self.filters_inverted]
        # Also filter selected:
        self.selected.intersection_update({t['id'] for t in self.torrents})

    def follow_list_focus(self):
        if self.focus == -1:
            return

        # check if list is empty or id to look for isn't in list
        ids = [t['id'] for t in self.torrents]
        if len(self.torrents) == 0 or self.focused_id not in ids:
            self.focus, self.scrollpos = -1, 0
            return

        # find focused_id
        self.focus = min(self.focus, len(self.torrents) - 1)
        if self.torrents[self.focus]['id'] != self.focused_id:
            for i, t in enumerate(self.torrents):
                if t['id'] == self.focused_id:
                    self.focus = i
                    break

        # make sure the focus is not above the visible area
        while self.focus < (self.scrollpos / gconfig.tlist_item_height):
            self.scrollpos -= gconfig.tlist_item_height
        # make sure the focus is not below the visible area
        while self.focus > (self.scrollpos / gconfig.tlist_item_height) + self.torrents_per_page - 1:
            self.scrollpos += gconfig.tlist_item_height
        # keep min and max bounds
        self.scrollpos = min(self.scrollpos, (len(self.torrents) - self.torrents_per_page) * gconfig.tlist_item_height)
        self.scrollpos = max(0, self.scrollpos)

    def torrent_text(self, t, search, details=[]):
        if search in ['fulltext', 'regex_fulltext']:
            s = t['name']
            if 'labels' in t:
                s += '; ' + ','.join(t['labels'])
            if 'commnets' in t:
                s += '; ' + t['comment']
            s += details[t['id']]
            return s.lower()
        return t['name'].lower()

    def get_torrents_filenames(self):
        self.server.set_torrent_details_id([t['id'] for t in self.torrents])
        self.server.wait_for_details_update()
        self.server.set_torrent_details_id(-1)
        return {t['id']: ', '.join(f['name'] for f in t['files']) for t in self.server.get_torrent_details()}

    def draw_torrent_list(self, search_keyword='', search='', refresh=True):
        self.torrents = self.server.get_torrent_list(gconfig.sort_orders)
        self.filter_torrent_list()

        if search_keyword and search:
            if search in ['fulltext', 'regex_fulltext']:
                torrents_files = self.get_torrents_filenames()
            else:
                torrents_files = None
            if search in ['pattern', 'fulltext']:
                matched_torrents = [t for t in self.torrents if search_keyword.lower() in self.torrent_text(t, search, torrents_files)]
            elif search in ['regex', 'regex_fulltext']:
                try:
                    regex = re.compile(search_keyword, re.I)
                    matched_torrents = [t for t in self.torrents if regex.search(self.torrent_text(t, search, torrents_files))]
                except Exception:
                    matched_torrents = self.torrents
            if matched_torrents:
                self.focus = 0
                if self.search_focus >= len(matched_torrents):
                    self.search_focus = 0
                if self.search_focus < 0:
                    self.search_focus = len(matched_torrents) - 1
                self.focused_id = matched_torrents[self.search_focus]['id']
                self.highlight_dialog = False
            else:
                self.highlight_dialog = True
                curses.beep()
        else:
            self.search_focus = 0
            self.highlight_dialog = False

        self.follow_list_focus()
        self.manage_layout()
        self.pad.erase()

        ypos = 0
        for i in range(len(self.visible_torrents)):
            ypos += self.draw_torrentlist_item(self.visible_torrents[i],
                                               (i == self.focus - self.visible_torrents_start),
                                               gconfig.tlist_item_height == 1,
                                               ypos, i + self.visible_torrents_start)
        if refresh:
            self.pad.refresh(0, 0, 1, 0, self.mainview_height, self.width - 1)
            self.screen.refresh()

    def draw_torrentlist_item(self, torrent, focused, compact, y, idx=-1):
        # the torrent name is also a progress bar
        selected = torrent['id'] in self.selected
        self.draw_torrentlist_title(torrent, focused, self.torrent_title_width, y, idx)

        if torrent['status'] == Transmission.STATUS_DOWNLOAD:
            self.draw_downloadrate(torrent, y, selected)
        if torrent['status'] == Transmission.STATUS_DOWNLOAD or torrent['status'] == Transmission.STATUS_SEED or selected:
            self.draw_uploadrate(torrent, y, selected)

        if not compact:
            # the line below the title/progress
            if torrent['percentDone'] < 100 and torrent['status'] == Transmission.STATUS_DOWNLOAD:
                self.draw_eta(torrent, y)

            self.draw_ratio(torrent, y, False)
            self.draw_torrentlist_status(torrent, focused, y)

            return gconfig.tlist_item_height  # number of lines that were used for drawing the list item
        # Draw ratio in place of upload rate if upload rate = 0
        if not torrent['rateUpload']:
            self.draw_ratio(torrent, y - 1, selected)

        return 1

    def draw_downloadrate(self, torrent, ypos, selected):
        tag = gconfig.element_attr('download_rate', st=selected)
        self.pad.move(ypos, self.width - self.rateDownload_width - self.rateUpload_width - 3)
        self.pad.addch(curses.ACS_DARROW, (0, curses.A_BOLD)[torrent['downloadLimited']])
        rate = ('', scale_bytes(torrent['rateDownload']))[torrent['rateDownload'] > 0]
        self.pad.addstr(rate.rjust(self.rateDownload_width), tag)

    def draw_uploadrate(self, torrent, ypos, selected):
        tag = gconfig.element_attr('upload_rate', st=selected)
        self.pad.move(ypos, self.width - self.rateUpload_width - 1)
        self.pad.addch(curses.ACS_UARROW, (0, curses.A_BOLD)[torrent['uploadLimited']])
        rate = ('', scale_bytes(torrent['rateUpload']))[torrent['rateUpload'] > 0]
        self.pad.addstr(rate.rjust(self.rateUpload_width), tag)

    def draw_ratio(self, torrent, ypos, selected):
        tag = gconfig.element_attr('eta+ratio', st=selected)
        self.pad.addch(ypos + 1, self.width - self.rateUpload_width - 1, curses.ACS_BULLET,
                       (0, curses.A_BOLD)[torrent['uploadRatio'] < 1 and torrent['uploadRatio'] >= 0])
        self.pad.addstr(ypos + 1, self.width - self.rateUpload_width,
                        num2str(torrent['uploadRatio'], '%.02f').rjust(self.rateUpload_width), tag)

    def draw_eta(self, torrent, ypos):
        self.pad.addch(ypos + 1, self.width - self.rateDownload_width - self.rateUpload_width - 3, curses.ACS_PLMINUS)
        self.pad.addstr(ypos + 1, self.width - self.rateDownload_width - self.rateUpload_width - 2,
                        scale_time(torrent['eta']).rjust(self.rateDownload_width),
                        gconfig.element_attr('eta+ratio'))

    def draw_torrentlist_title(self, torrent, focused, width, ypos, idx):
        if gconfig.torrent_numbers and idx >= 0:
            numwidth = len("%i" % (len(self.torrents) + 1))
            width = width - numwidth - 1
            self.pad.addstr(ypos, 0, str(int(idx + 1)).rjust(numwidth)+' ')
        else:
            self.pad.move(ypos, 0)
        if torrent['status'] == Transmission.STATUS_SEED or (torrent['percentDone'] == 100 and torrent['status'] == Transmission.STATUS_STOPPED):
            if torrent['seedRatioMode'] == 0:  # Use global limit if set, otherwise unlimited
                limit = self.stats['seedRatioLimit'] if self.stats['seedRatioLimited'] else -1
            elif torrent['seedRatioMode'] == 1:  # Stop seeding at seedRatioLimit
                limit = torrent['seedRatioLimit']
            elif torrent['seedRatioMode'] == 2:  # Seed regardless of ratio
                limit = -1

            if limit > 0:
                percentDone = min((torrent['uploadRatio'] * 100) / limit, 100)
            elif limit < 0:
                percentDone = 100
            else:
                percentDone = 0

        elif torrent['status'] == Transmission.STATUS_CHECK:
            percentDone = float(torrent['recheckProgress']) * 100
        else:
            percentDone = torrent['percentDone']

        str_f = "%s" if self.narrow else "%7s"

        size = str_f % scale_bytes(torrent['sizeWhenDone'])
        if torrent['percentDone'] < 100:
            if torrent['seeders'] <= 0 and torrent['status'] != Transmission.STATUS_CHECK:
                size = str_f % scale_bytes(torrent['available']) + "/" + size
            size = str_f % scale_bytes(torrent['haveValid'] + torrent['haveUnchecked']) + "/" + size
        size = '| ' + size
        title = ljust_columns(torrent['name'], width - len(size)) + size

        if torrent['isIsolated']:
            element_name = 'title_error'
        elif torrent['status'] == Transmission.STATUS_SEED or \
                torrent['status'] == Transmission.STATUS_SEED_WAIT:
            element_name = 'title_seed'
        elif torrent['status'] == Transmission.STATUS_STOPPED:
            element_name = 'title_paused_done' if torrent['percentDone'] == 100 else 'title_paused'
        elif torrent['status'] == Transmission.STATUS_CHECK or \
                torrent['status'] == Transmission.STATUS_CHECK_WAIT:
            element_name = 'title_verify'
        elif torrent['rateDownload'] == 0:
            element_name = 'title_idle'
        elif torrent['percentDone'] < 100:
            element_name = 'title_download'
        else:
            element_name = 'title_other'

        tag = gconfig.element_attr(element_name+'_incomp')
        tag_done = gconfig.element_attr(element_name)
        if focused:
            tag += curses.A_BOLD
            tag_done += curses.A_BOLD

        if gconfig.torrentname_is_progressbar:
            bar_width = int(float(width) * (float(percentDone) / 100))
            # Estimate widths, which works for anything ASCII
            bar_complete = title[:bar_width]
            bar_incomplete = title[bar_width:]
            # Adjust for East-Asian (wide) characters
            while not bar_width - 1 <= len_columns(bar_complete) <= bar_width:
                if len_columns(bar_complete) > bar_width:
                    bar_incomplete = bar_complete[-1] + bar_incomplete
                    bar_complete = bar_complete[:-1]
                else:
                    bar_complete += bar_incomplete[0]
                    bar_incomplete = bar_incomplete[1:]
            self.pad.addstr(bar_complete, tag_done)
            self.pad.addstr(bar_incomplete, tag)
        else:
            self.pad.addstr(title, tag_done)

    def draw_torrentlist_status(self, torrent, focused, ypos):
        peers = ''
        parts = [self.server.get_status(torrent, self.narrow)]

        if torrent['isIsolated'] and torrent['peersConnected'] <= 0:
            if not torrent['trackerStats']:
                parts[0] = "No tracker and no DHT" if self.narrow else "Unable to find peers without trackers and DHT disabled"
            else:
                tracker_errors = [tracker['lastAnnounceResult'] or tracker['lastScrapeResult']
                                  for tracker in torrent['trackerStats']]
                parts[0] = [te for te in tracker_errors if te][0]
        else:
            pct_f = "%.0f%%" if self.narrow else " (%.2f%%)"
            if torrent['status'] == Transmission.STATUS_CHECK:
                parts[0] += pct_f % (torrent['recheckProgress'] * 100)
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
                if torrent['metadataPercentComplete'] < 1:
                    parts[0] += pct_f % (torrent['metadataPercentComplete'] * 100)
                else:
                    parts[0] += pct_f % torrent['percentDone']
            if not self.narrow:
                parts[0] = parts[0].ljust(20)

            # seeds and leeches will be appended right justified later
            if self.narrow:
                peers = "s:%s l:%s" % (num2str(torrent['seeders']), num2str(torrent['leechers']))
            else:
                peers = "%5s seed%s " % (num2str(torrent['seeders']), ('s', ' ')[torrent['seeders'] == 1])
                peers += "%5s leech%s" % (num2str(torrent['leechers']), ('es', '  ')[torrent['leechers'] == 1])

            # show additional information if enough room
            if self.narrow:
                if self.torrent_title_width - sum([len(x) for x in parts]) - len(peers) > 9 and torrent['uploadedEver'] > 0:
                    uploaded = scale_bytes(torrent['uploadedEver'])
                    parts.append("U:%s" % uploaded)
                if self.torrent_title_width - sum([len(x) for x in parts]) - len(peers) > 6:
                    parts.append("P:%d" % torrent['peersConnected'])
            else:
                if self.torrent_title_width - sum([len(x) for x in parts]) - len(peers) > 18:
                    uploaded = scale_bytes(torrent['uploadedEver'])
                    parts.append("%7s uploaded" % ('nothing', uploaded)[uploaded != '0B'])

                if self.torrent_title_width - sum([len(x) for x in parts]) - len(peers) > 12:
                    parts.append("%4s peer%s" % (torrent['peersConnected'],
                                                           ('s', ' ')[torrent['peersConnected'] == 1]))

        if focused:
            tags = curses.A_REVERSE + curses.A_BOLD
        else:
            tags = 0

        remaining_space = self.torrent_title_width - sum([len(x) for x in parts], len(peers)) - 3
        delimiter = ' ' * int(remaining_space / (len(parts)))

        line = self.server.get_bandwidth_priority(torrent) + self.server.get_honors_session_limits(torrent) + ' ' + delimiter.join(parts)

        # make sure the peers element is always right justified
        line += ' ' * int(self.torrent_title_width - len(line) - len(peers)) + peers
        self.pad.addstr(ypos + 1, 0, line, tags)

    def draw_details(self, search_keyword=None, search='', refresh=True):
        self.torrent_details = self.server.get_torrent_details()
        self.manage_layout()
        self.pad.erase()

        # torrent name + progress bar
        self.draw_torrentlist_item(self.torrent_details, False, False, 0)

        # divider + menu
        menu_items = ['_Overview', "_Files", 'P_eers', '_Trackers', '_Chunks']
        xpos = max(0, int((self.width - sum([len(x) for x in menu_items]) - len(menu_items)) / 2))
        for item in menu_items:
            self.pad.move(3, xpos)
            tags = curses.A_BOLD
            if menu_items.index(item) == self.details_category_focus:
                tags += curses.A_REVERSE
            title = item.split('_')
            self.pad.addstr(title[0], tags)
            self.pad.addstr(title[1][0], tags + curses.A_UNDERLINE)
            self.pad.addstr(title[1][1:], tags)
            xpos += len(item) + 1

        # which details to display
        if self.details_category_focus == 0:
            self.draw_details_overview(5)
        elif self.details_category_focus == 1:
            if search_keyword:
                self.draw_filelist_search(search_keyword, search=search)
            else:
                self.draw_filelist(5)
        elif self.details_category_focus == 2:
            self.draw_peerlist(5)
        elif self.details_category_focus == 3:
            self.draw_trackerlist(5)
        elif self.details_category_focus == 4:
            self.draw_pieces_map(5)

        if refresh:
            self.pad.refresh(0, 0, 1, 0, self.mainview_height, self.width)
            self.screen.refresh()

    def draw_details_overview(self, ypos):
        t = self.torrent_details
        if self.narrow:
            strings = ['C: ', 'D: ', 'U: ',
                       '<waiting for %d peers, %d done>',
                       'Swarm: ',
                       '<no downloading peers>',
                       '%s',
                       'unlimited',
                       'BW limits: ',
                       'D: ',
                       'U: ',
                       'Private torrent',
                       ]
        else:
            strings = ['connected to ',
                       'downloading from ',
                       'uploading to ',
                       '<gathering info from %d peers, %d done>',
                       'Swarm speed: ',
                       '<no downloading peers connected>',
                       'pause torrent after distributing %s copies',
                       'unlimited (ignore global limits)',
                       'Bandwidth limits: ',
                       'Download: ',
                       'Upload: ',
                       'Private to this tracker -- DHT and PEX disabled',
                       ]
        info = []
        info.append(['Hash: ', "%s" % t['hashString']])
        info.append(['ID: ', "%s" % t['id']])

        wanted = 0
        for i, _ in enumerate(t['files']):
            if t['wanted'][i]:
                wanted += t['files'][i]['length']

        sizes = ['Size: ', "%s;  " % scale_bytes(t['totalSize'], long=True),
                 "%s wanted;  " % (scale_bytes(wanted, long=True), 'everything')[t['totalSize'] == wanted]]
        if t['available'] < t['totalSize']:
            sizes.append("%s available;  " % scale_bytes(t['available'], long=True))
        sizes.extend(["%s left" % scale_bytes(t['leftUntilDone'], long=True)])
        info.append(sizes)

        info.append(['Files: ', "%d;  " % len(t['files'])])
        complete = list(map(lambda x: x['bytesCompleted'] == x['length'], t['files'])).count(True)
        not_complete = [x for x in t['files'] if x['bytesCompleted'] != x['length']]
        partial = list(map(lambda x: x['bytesCompleted'] > 0, not_complete)).count(True)
        if complete == len(t['files']):
            info[-1].append("all complete")
        else:
            info[-1].append("%d complete;  " % complete)
            info[-1].append("%d commenced" % partial)

        info.append(['Chunks: ', "%s;  " % t['pieceCount'],
                     "%s each" % scale_bytes(t['pieceSize'], long=True)])

        info.append(['Download: '])
        info[-1].append("%s" % scale_bytes(t['downloadedEver'], long=True)
                        + " (%.2f%%) received;  " % percent(t['sizeWhenDone'], t['downloadedEver']))
        info[-1].append("%s" % scale_bytes(t['haveValid'], long=True)
                        + " (%.2f%%) verified;  " % percent(t['sizeWhenDone'], t['haveValid']))
        info[-1].append("%s corrupt" % scale_bytes(t['corruptEver'], long=True))
        if t['percentDone'] < 100:
            info[-1][-1] += ';  '
            if t['rateDownload']:
                info[-1].append("receiving %s per second" % scale_bytes(t['rateDownload'], long=True))
                if t['downloadLimited']:
                    info[-1][-1] += " (throttled to %s)" % scale_bytes(t['downloadLimit'] * 1024, long=True)

        try:
            copies_distributed = (float(t['uploadedEver']) / float(t['sizeWhenDone']))
        except ZeroDivisionError:
            copies_distributed = 0
        info.append(['Upload: ', "%s (%.2f%%) transmitted" %
                     (scale_bytes(t['uploadedEver'], long=True), t['uploadRatio'] * 100)])
        if t['rateUpload']:
            info.append(" Sending %s per second" % scale_bytes(t['rateUpload'], long=True))
            if t['uploadLimited']:
                info[-1] += " (throttled to %s)" % scale_bytes(t['uploadLimit'] * 1024, long=True)

        info.append(['Ratio: ', '%.2f copies distributed' % copies_distributed])
        norm_upload_rate = norm.add('%s:rateUpload' % t['id'], t['rateUpload'], 50)
        format_str = "%X" if self.narrow else "%x %X"
        if norm_upload_rate > 0:
            target_ratio = self.get_target_ratio()
            bytes_left = (max(t['downloadedEver'], t['sizeWhenDone']) * target_ratio) - t['uploadedEver']
            time_left = bytes_left / norm_upload_rate
            info.append(' Approaching %.2f ... %s' % (target_ratio, timestamp(time.time() + time_left, narrow=self.narrow, time_format=format_str)))

        info.append(['Seed limit: '])
        if t['seedRatioMode'] == 0:
            if self.stats['seedRatioLimited']:
                info[-1].append('default (' + strings[6] % self.stats['seedRatioLimit'] + ')')
            else:
                info[-1].append('default (unlimited)')
        elif t['seedRatioMode'] == 1:
            info[-1].append(strings[6] % t['seedRatioLimit'])
        elif t['seedRatioMode'] == 2:
            info[-1].append(strings[7])

        info.append([strings[8]])
        unlimited = 'session' if t['honorsSessionLimits'] else 'unlimited'
        info[-1].append(strings[9] + (scale_bytes(t['downloadLimit'] * 1024) if t['downloadLimited'] else unlimited) + ";  ")
        info[-1].append(strings[10] + (scale_bytes(t['uploadLimit'] * 1024) if t['uploadLimited'] else unlimited))

        info.append(['Peers: ',
                     strings[0] + "%d;  " % t['peersConnected'],
                     strings[1] + "%d;  " % t['peersSendingToUs'],
                     strings[2] + "%d" % t['peersGettingFromUs']])

        # average peer speed
        incomplete_peers = [peer for peer in self.torrent_details['peers'] if peer['progress'] < 1]
        if incomplete_peers:
            # use at least 2/3 or 10 of incomplete peers to make an estimation
            active_peers = [peer for peer in incomplete_peers if peer['download_speed']]
            min_active_peers = min(10, max(1, round(len(incomplete_peers) * 0.666)))
            if 1 <= len(active_peers) >= min_active_peers:
                swarm_speed = sum([peer['download_speed'] for peer in active_peers]) / len(active_peers)
                info.append(['Swarm speed: ', "%s on average;  " % scale_bytes(swarm_speed),
                             "distribution of 1 copy takes %s" %
                             scale_time(int(t['totalSize'] / swarm_speed), long=True)])
            else:
                info.append(['Swarm speed: ', strings[3] %
                             (min_active_peers, len(active_peers))])
        else:
            info.append([strings[4], strings[5]])

        info.append(['Privacy: '])
        if t['isPrivate']:
            info[-1].append('Private to this tracker -- DHT and PEX disabled')
        else:
            info[-1].append('Public torrent')

        info.append(['Location: ', "%s" % homedir2tilde(t['downloadDir'])])

        if t['creator']:
            info.append(['Creator: ', "%s" % t['creator']])

        if 'labels' in t and t['labels']:
            info.append(['Labels: '])
            info[-1].extend([s + '; ' for s in t['labels'][:-1]])
            info[-1].append(t['labels'][-1])

        if 'group' in t and t['group']:
            info.append(['Group: ', t['group']])

        info.append([''])

        info.append(['Created: ', "%s" % timestamp(t['dateCreated'], narrow=self.narrow, time_format=format_str)])
        info.append(['Added: ', "%s" % timestamp(t['addedDate'], narrow=self.narrow, time_format=format_str)])
        info.append(['Started: ', "%s" % timestamp(t['startDate'], narrow=self.narrow, time_format=format_str)])
        info.append(['Activity: ', "%s" % timestamp(t['activityDate'], narrow=self.narrow, time_format=format_str)])

        if t['percentDone'] < 100 and t['eta'] > 0:
            info.append(['Finishing: ', "%s" % timestamp(time.time() + t['eta'], narrow=self.narrow, time_format=format_str)])
        elif t['doneDate'] <= 0:
            info.append(['Finishing: ', 'sometime'])
        else:
            info.append(['Finished: ', "%s" % timestamp(t['doneDate'], narrow=self.narrow, time_format=format_str)])

        if t['comment']:
            info.append([''])
            info.append(['Comment: ', t['comment']])

        ypos = self.draw_details_list(ypos, info)

        self.max_overview_scroll = max(self.max_overview_scroll, ypos + self.scrollpos_detaillist[0] - self.height + 3)

        return ypos + 1

    def get_target_ratio(self):
        t = self.torrent_details
        if t['seedRatioMode'] == 1:
            return t['seedRatioLimit']              # individual limit
        if t['seedRatioMode'] == 0 and self.stats['seedRatioLimited']:
            return self.stats['seedRatioLimit']     # global limit
        # round up to next 10/5/1
        if t['uploadRatio'] >= 100:
            step_size = 10.0
        elif t['uploadRatio'] >= 10:
            step_size = 5.0
        else:
            step_size = 1.0
        return int(round((t['uploadRatio'] + step_size / 2) / step_size) * step_size)

    def draw_filelist_search(self, search_keyword=None, search=''):
        if search_keyword and search:
            if search == 'pattern':
                matched_files = [f for f in self.sorted_files if search_keyword.lower() in os.path.basename(f['name'].lower())]
            elif search == 'regex':
                try:
                    regex = re.compile(search_keyword, re.I)
                    matched_files = [f for f in self.sorted_files if regex.search(os.path.basename(f['name']))]
                except Exception:
                    matched_files = self.sorted_files
            if matched_files:
                if self.search_focus >= len(matched_files):
                    self.search_focus = 0
                if self.search_focus < 0:
                    self.search_focus = len(matched_files) - 1
                self.focus_detaillist = self.sorted_files.index(matched_files[self.search_focus])
                self.highlight_dialog = False
            else:
                self.highlight_dialog = True
                curses.beep()
        else:
            self.search_focus = 0
            self.highlight_dialog = False
        self.draw_filelist(5, needclrtoeol=True)
        self.pad.refresh(0, 0, 1, 0, self.mainview_height, self.width)
        self.screen.refresh()

    def draw_filelist(self, ypos, needclrtoeol=False):
        column_names = '   #  Progress    Size  Priority  Filename'
        self.pad.addstr(ypos, 0, column_names.ljust(self.width), curses.A_UNDERLINE)
        ypos += 1

        for line, sel, focus in self.create_filelist():
            curses_tags = 0
            # highlight focused/selected line(s)
            if sel:
                curses_tags = gconfig.selected_file_attr
            if focus:
                curses_tags += curses.A_REVERSE
            try:
                self.pad.addstr(ypos, 0, ' ' * self.width, curses_tags)
            except Exception as e:
                pdebug('caught %s in draw_filelist(): %s\n' % (type(e), str(e)))

            # 25 chars before priority, 6 chars priority, 2 chars skipped
            priority = line[25:31].strip()
            # Except if the file index has more than 4 digits:
            if priority[:4] == 'norm':
                priority = 'normal'
            priority_start = 28 - (len(priority) + 1) // 2
            priority_end = priority_start + len(priority)
            if priority:
                priority_tag = curses_tags + gconfig.element_attr('file_prio_' + priority)
            else:
                priority_tag = curses_tags
            self.pad.addstr(ypos, 0, line[0:priority_start], curses_tags)
            self.pad.addstr(ypos, priority_start, line[priority_start:priority_end], priority_tag)
            self.pad.addstr(ypos, priority_end, line[priority_end:33], curses_tags)
            self.pad.addstr(ypos, 33, line[33:], curses_tags)
            if needclrtoeol:
                self.pad.addstr(' ' * (self.width - self.pad.getyx()[1]), curses_tags)
            ypos += 1
            if ypos > self.height:
                break

    def create_filelist(self):
        # Build new mapping between sorted local files and transmission-daemon's unsorted files.
        if self.filelist_needs_refresh:
            self.filelist_needs_refresh = False
            if self.focus_detaillist > -1:
                # focus_detaillist is the file index in visible list, in order
                # to facilitate movement and display. So save name in order to
                # find the new position of the file later.
                focused_filename = self.torrent_details['files'][self.file_index_map[self.focus_detaillist]]['name']
            self.file_index_map = {}
            if gconfig.file_sort_key in ['name', 'length', 'bytesCompleted']:
                self.sorted_files = sorted(self.torrent_details['files'], key=lambda x: x[gconfig.file_sort_key], reverse=gconfig.file_sort_reverse)
            elif gconfig.file_sort_key == 'progress':
                self.sorted_files = sorted(self.torrent_details['files'], key=lambda x: x['bytesCompleted'] / x['length'] if x['length'] > 0 else 0, reverse=gconfig.file_sort_reverse)
            else:
                if gconfig.file_sort_reverse:
                    self.sorted_files = list(reversed(self.torrent_details['files']))
                else:
                    self.sorted_files = self.torrent_details['files'][:]
            for index, file in enumerate(self.sorted_files):
                self.file_index_map[index] = self.torrent_details['files'].index(file)
            # Find the focused file in new sorted list. First check if it is in
            # the same index, as that is the most common case.
            if self.focus_detaillist > -1 and focused_filename != self.torrent_details['files'][self.file_index_map[self.focus_detaillist]]['name']:
                self.focus_detaillist = next(i for i in range(len(self.sorted_files)) if
                                             self.torrent_details['files'][self.file_index_map[i]]['name'] == focused_filename)

            self.filelist_cache = []
            self.filelist_cache_pos = []
            self.filelist_cache_pos_dict = dict()
            current_folder = []
            current_depth = 0
            pos = 0
            pos_before_focus = 0
            index = 0
            for file in self.sorted_files:
                f = file['name'].split('/')
                f_len = len(f) - 1
                if f[:f_len] != current_folder:
                    [current_depth, pos] = self.create_filelist_transition(f, current_folder, self.filelist_cache, current_depth, pos)
                    current_folder = f[:f_len]
                self.filelist_cache.append(self.create_filelist_line(f[-1], index, percent(file['length'], file['bytesCompleted']),
                                                                     file['length'], current_depth))
                self.filelist_cache_pos.append(pos)
                self.filelist_cache_pos_dict[index + pos] = index
                index += 1

        if self.focus_detaillist == -1:
            start = 0
            end = min(self.detaillines_per_page, len(self.filelist_cache))
            line_to_show = -1
        else:
            pos_before_focus = self.filelist_cache_pos[self.focus_detaillist]
            line_to_show = self.focus_detaillist + pos_before_focus
            lines_before = self.detaillines_per_page // 2
            if line_to_show >= lines_before:
                start = line_to_show - lines_before
            else:
                start = 0
            if len(self.filelist_cache) >= start + self.detaillines_per_page:
                end = start + self.detaillines_per_page
            else:
                end = len(self.filelist_cache)
                start = end - self.detaillines_per_page if end >= self.detaillines_per_page else 0
        ret = []
        for i in range(start, end):
            ret.append((
                self.filelist_cache[i],
                i in self.filelist_cache_pos_dict and self.filelist_cache_pos_dict[i] in self.selected_files,
                i == line_to_show
            ))
        return ret

    def create_filelist_transition(self, f, current_folder, filelist, current_depth, pos):
        """ Create directory transition from <current_folder> to <f>,
        both of which are an array of strings, each one representing one
        subdirectory in their path (e.g. /tmp/a/c would result in
        [temp, a, c]). <filelist> is a list of strings that will later be drawn
        to screen. This function only creates directory strings, and is
        responsible for managing depth (i.e. indentation) between different
        directories.
        """
        f_len = len(f) - 1  # Amount of subdirectories in f
        current_folder_len = len(current_folder)  # Amount of subdirectories in
        # current_folder
        # Number of directory parts from f and current_directory that are identical
        same = 0
        while (same < current_folder_len and
               same < f_len and
               f[same] == current_folder[same]):
            same += 1

        for _ in range(current_folder_len - same):
            current_depth -= 1

        # Stepping out of a directory, but not into a new directory
        if f_len < current_folder_len and f_len == same:
            return [current_depth, pos]

        # Increase depth for each new directory that appears in f,
        # but not in current_directory
        while current_depth < f_len:
            filelist.append('%s\\ %s' % ('  ' * current_depth + ' ' * 34, f[current_depth]))
            current_depth += 1
            pos += 1
        return [current_depth, pos]

    def create_filelist_line(self, name, index, percent, length, current_depth):
        line = "%s  %6.2f%%" % (str(index + 1).rjust(4), percent) + \
            '  ' + scale_bytes(length).rjust(7) + \
            '  ' + self.server.get_file_priority(self.torrent_details['id'], self.file_index_map[index]).center(8) + \
            " %s| %s" % ('  ' * current_depth, name[0:self.width - 34 - current_depth])
        return line

    def draw_peerlist(self, ypos):
        # Start drawing list either at the "selected" index, or at the index
        # that is required to display all remaining items without further scrolling.
        last_possible_index = max(0, len(self.torrent_details['peers']) - self.detaillines_per_page)
        start = min(self.scrollpos_detaillist[2], last_possible_index)
        end = start + self.detaillines_per_page
        peers = self.torrent_details['peers'][start:end]

        # Find width of columns
        clientname_width = 0
        address_width = 0
        port_width = 0
        for peer in peers:
            if len(peer['clientName']) > clientname_width:
                clientname_width = len(peer['clientName'])
            if len(peer['address']) > address_width:
                address_width = len(peer['address'])
            if len(str(peer['port'])) > port_width:
                port_width = len(str(peer['port']))

        # Column names
        column_names = 'Flags   %3d Down   %3d Up Progress           ETA ' % \
            (self.torrent_details['peersSendingToUs'], self.torrent_details['peersGettingFromUs'])
        column_names += 'Client'.ljust(clientname_width + 1) \
            + 'Address'.ljust(address_width + port_width + 1)
        column_names += ' Country'
        if gconfig.rdns:
            column_names += ' Host'

        self.pad.addnstr(ypos, 0, column_names.ljust(self.width), self.width, curses.A_UNDERLINE)
        ypos += 1

        # Peers
        hosts = self.server.get_hosts()
        geo_ips = self.server.get_geo_ips()
        for _, peer in enumerate(peers):
            if gconfig.rdns:
                if peer['address'] in hosts:
                    host_name = hosts[peer['address']]
                else:
                    host_name = "<resolving>"

            upload_tag = download_tag = 0
            if peer['rateToPeer']:
                upload_tag = curses.A_BOLD
            if peer['rateToClient']:
                download_tag = curses.A_BOLD

            self.pad.move(ypos, 0)
            # Flags
            self.pad.addstr("%-6s   " % peer['flagStr'])
            # Down
            self.pad.addstr("%7s  " % scale_bytes(peer['rateToClient']), download_tag)
            # Up
            self.pad.addstr("%7s " % scale_bytes(peer['rateToPeer']), upload_tag)

            # Progress
            if peer['progress'] < 1:
                self.pad.addstr("%7.2f%%" % (float(peer['progress']) * 100))
            else:
                self.pad.addstr("%7.2f%%" % (float(peer['progress']) * 100), curses.A_BOLD)

            # ETA
            if self.width >= 55:
                if peer['progress'] < 1 and peer['download_speed'] > 1024:
                    self.pad.addstr(" %8s %4s " %
                                    ('~' + scale_bytes(peer['download_speed']),
                                     '~' + scale_time(peer['time_left'])))
                else:
                    if peer['progress'] < 1:
                        self.pad.addstr("   <guessing>  ")
                    else:
                        self.pad.addstr("               ")
            # Client
            if self.width >= 55 + clientname_width + 1:
                self.pad.addstr(peer['clientName'].ljust(clientname_width + 1))
            # Address:Port
            if self.width >= 55 + clientname_width + address_width + port_width + 3:
                self.pad.addstr(peer['address'].rjust(address_width)
                                + ':' + str(peer['port']).ljust(port_width) + ' ')
            # Country
            if self.width >= 55 + clientname_width + address_width + port_width + 3 + 7:
                self.pad.addstr("  %2s   " % geo_ips.get(peer['address'], '--'))
            # Host
            if self.width >= 55 + clientname_width + address_width + port_width + 3 + 10:
                if gconfig.rdns:
                    self.pad.addnstr(host_name, self.width - self.pad.getyx()[1], curses.A_DIM)
            ypos += 1

    def draw_trackerlist(self, ypos):
        top = ypos - 1

        def addstr(ypos, xpos, *args):
            if top < ypos < self.mainview_height:
                self.pad.addstr(ypos, xpos, *args)

        tracker_per_page = max(1, self.detaillines_per_page // (self.TRACKER_ITEM_HEIGHT + 2))
        page = self.scrollpos_detaillist[3] // tracker_per_page
        start = tracker_per_page * page
        end = tracker_per_page * (page + 1)
        tlist = self.torrent_details['trackerStats'][start:end]

        # keep position in range when last tracker gets deleted
        self.scrollpos_detaillist[3] = min(self.scrollpos_detaillist[3],
                                           len(self.torrent_details['trackerStats']) - 1)
        # show newly added tracker when list was empty before
        if self.torrent_details['trackerStats']:
            self.scrollpos_detaillist[3] = max(0, self.scrollpos_detaillist[3])

        current_tier = -1
        for _, t in enumerate(tlist):
            announce_msg_size = scrape_msg_size = 0
            selected = t == self.torrent_details['trackerStats'][self.scrollpos_detaillist[3]]

            if current_tier != t['tier']:
                current_tier = t['tier']

                tiercolor = curses.A_BOLD + curses.A_REVERSE \
                    if selected else curses.A_REVERSE
                addstr(ypos, 0, ("Tier %d" % (current_tier + 1)).ljust(self.width), tiercolor)
                ypos += 1

            if selected:
                for i in range(self.TRACKER_ITEM_HEIGHT):
                    addstr(ypos + i, 0, ' ', curses.A_BOLD + curses.A_REVERSE)

            format_str = "%X" if self.narrow else "%x %X"
            addstr(ypos + 1, 4, "Last announce: %s" % timestamp(t['lastAnnounceTime'], narrow=self.narrow, time_format=format_str))
            addstr(ypos + 2, 4, "Next announce: %s" % timestamp(t['nextAnnounceTime'], narrow=self.narrow, time_format=format_str))
            addstr(ypos + 3, 4, "  Last scrape: %s" % timestamp(t['lastScrapeTime'], narrow=self.narrow, time_format=format_str))
            addstr(ypos + 4, 4, "  Next scrape: %s" % timestamp(t['nextScrapeTime'], narrow=self.narrow, time_format=format_str))

            if t['lastScrapeSucceeded']:
                if self.narrow:
                    seeds = "S:%s" % num2str(t['seederCount'])
                    leeches = "P:%s" % num2str(t['leecherCount'])
                else:
                    seeds = "%s seed%s" % (num2str(t['seederCount']), ('s', '')[t['seederCount'] == 1])
                    leeches = "%s leech%s" % (num2str(t['leecherCount']), ('es', '')[t['leecherCount'] == 1])
                addstr(ypos + 5, 4, "Tracker knows: %s and %s" % (seeds, leeches), curses.A_BOLD)
            else:
                if t['lastScrapeResult']:
                    if self.narrow:
                        addstr(ypos + 5, 11, "Scrape: %s" % t['lastScrapeResult'].replace("Tracker gave HTTP response code ", "")[:self.width - 20])
                    else:
                        addstr(ypos + 5, 11, "Scrape: %s" % t['lastScrapeResult'][:self.width - 20])

            if t['lastAnnounceSucceeded']:
                peers = "%s peer%s" % (num2str(t['lastAnnouncePeerCount']), ('s', '')[t['lastAnnouncePeerCount'] == 1])
                addstr(ypos, 2, t['announce'][:self.width - 2], curses.A_BOLD + curses.A_UNDERLINE)
                addstr(ypos + 6, 11, "Result: %s received" % peers, curses.A_BOLD)
            else:
                addstr(ypos, 2, t['announce'][:self.width - 2], curses.A_UNDERLINE)
                if t['lastAnnounceResult']:
                    if self.narrow:
                        addstr(ypos + 6, 9, "Announce: %s" % t['lastAnnounceResult'].replace("Tracker gave HTTP response code ", "")[:self.width - 20])
                    else:
                        addstr(ypos + 6, 9, "Announce: %s" % t['lastAnnounceResult'][:self.width - 20])

            ypos += max(announce_msg_size, scrape_msg_size)

            ypos += 7

    def draw_pieces_map(self, ypos):
        if self.torrent_details['totalSize'] == 0:
            # No pieces in file of size 0
            return
        elif self.torrent_details['haveValid'] / self.torrent_details['totalSize'] < 0.5:
            default_attr = gconfig.element_attr('chunk_dont_have')
            new_attr = gconfig.element_attr('chunk_have')
            change_attr = 0x80
            skip_run = 0
        else:
            new_attr = gconfig.element_attr('chunk_dont_have')
            default_attr = gconfig.element_attr('chunk_have')
            change_attr = 0
            skip_run = 255
        pieces = self.torrent_details['pieces']
        piece_count = self.torrent_details['pieceCount']
        margin = len(str(piece_count)) + 2
        map_width = (self.width - margin - 1) // 10 * 10
        start = self.scrollpos_detaillist[4] * map_width
        end = min(start + (self.height - ypos - 3) * map_width, piece_count)
        last_line = (end - 1) // map_width
        if end <= start:
            return

        for x in range(10, map_width, 10):
            self.pad.addstr(ypos, x + margin - 1, str(x), curses.A_BOLD)

        format_str = "%%%dd" % (margin - 2)
        yp = ypos + 1
        for counter in range(self.scrollpos_detaillist[4], last_line + 1):
            self.pad.addstr(yp, 1, format_str % (counter * map_width), curses.A_BOLD)
            if counter == last_line:
                self.pad.addstr(yp, margin, '-' * ((end - 1) % map_width + 1), default_attr)
            else:
                self.pad.addstr(yp, margin, '-' * map_width, default_attr)
            yp = yp + 1

        counter = start
        block = (pieces[start >> 3]) << (start & 7)
        while counter < end:
            if counter & 7 == 0:
                block = (pieces[counter >> 3])
                while block == skip_run and counter < end - 8:
                    counter += 8
                    block = (pieces[counter >> 3])
            if block & 0x80 == change_attr:
                self.pad.chgat(ypos + 1 + (counter-start) // map_width, margin + (counter-start) % map_width, 1, new_attr)
            block <<= 1
            counter += 1
        if counter >= end:
            counter = end - 1

        missing_pieces = piece_count - counter - 1
        if missing_pieces:
            line = "-- %d more --" % (missing_pieces)
            xpos = (self.width - len(line)) / 2
            self.pad.addstr(int(self.height - 3), int(xpos), line, curses.A_REVERSE)

    def draw_details_list(self, ypos, info):
        yp = ypos - self.scrollpos_detaillist[0]
        if self.narrow:
            key_width = 1
        else:
            key_width = max([len(x[0]) for x in info])

        self.pad.move(ypos, 0)
        for i in info:
            xp = 0
            if self.narrow and i[0] == 'Hash: ' and self.width < 46:
                value_x = 0
            else:
                if i[0] == 'Comment: ':
                    i = [i[0]] + list(wrap_multiline(i[1], self.width - 1, initial_indent=i[0].rjust(key_width + 1)))
                    # Ugly but does the work - wrapping takes key into
                    # account, but the actual text must not include it:
                    i[1] = i[1][len(i[0].rjust(key_width + 1)):]
                if yp >= ypos:
                    if self.narrow:
                        self.pad.addstr(yp, 0, i[0].rjust(key_width), curses.A_BOLD)  # key
                    else:
                        self.pad.addstr(yp, 1, i[0].rjust(key_width))  # key
                    xp = key_width
                value_x = key_width
            # value part may be wrapped if it gets too long
            for v in i[1:]:
                if xp + len(v) >= self.width - 1:
                    yp += 1
                    if yp >= ypos:
                        self.pad.move(yp, value_x)
                    xp = value_x
                if yp >= ypos:
                    self.pad.addnstr(v, self.width - value_x)
                xp += len(v)
            yp += 1
            if yp > self.mainview_height:
                return yp
        return yp

    def action_next_details(self):
        if self.details_category_focus >= 4:
            self.details_category_focus = 0
        else:
            self.details_category_focus += 1
        if self.details_category_focus == 1:  # We moved to file list
            self.filelist_needs_refresh = True
        self.focus_detaillist = -1
        self.pad.erase()

    def action_prev_details(self):
        if self.details_category_focus <= 0:
            self.details_category_focus = 4
        else:
            self.details_category_focus -= 1
        if self.details_category_focus == 1:  # We moved to file list
            self.filelist_needs_refresh = True
        self.pad.erase()

    def move_up(self, focus, scrollpos, step_size):
        if focus < 0:
            focus = -1
        else:
            focus -= 1
            if scrollpos / step_size - focus > 0:
                scrollpos -= step_size
                scrollpos = max(0, scrollpos)
            while scrollpos % step_size:
                scrollpos -= 1
        return focus, scrollpos

    def move_down(self, focus, scrollpos, step_size, elements_per_page, list_height):
        if focus < list_height - 1:
            focus += 1
            if focus + 1 - scrollpos / step_size > elements_per_page:
                scrollpos += step_size
        return focus, scrollpos

    def move_page_up(self, focus, scrollpos, step_size, elements_per_page):
        focus = max(0, focus - elements_per_page + 1)
        scrollpos = max(0, scrollpos - (elements_per_page - 1) * step_size)
        return focus, scrollpos

    def move_page_down(self, focus, scrollpos, step_size, elements_per_page, list_height):
        focus += (elements_per_page - 1)
        scrollpos += (elements_per_page - 1) * step_size
        if focus >= list_height:
            scrollpos -= (focus - list_height + 1) * step_size
            focus = list_height - 1
        return focus, scrollpos

    def move_to_top(self):
        return 0, 0

    def move_to_end(self, step_size, elements_per_page, list_height):
        focus = list_height - 1
        scrollpos = max(0, (list_height - elements_per_page) * step_size)
        return focus, scrollpos

    def draw_stats(self):
        try:
            self.screen.addstr(self.height - 1, 0, ' '.center(self.width), gconfig.element_attr('bottom_line'))
        except curses.error:
            # curses can print to the last char (bottom right corner), but it raises an exception.
            pass
        self.draw_torrents_stats()
        self.draw_global_rates()

    def draw_torrents_stats(self):
        if self.selected_torrent > -1 and self.details_category_focus == 2:
            self.screen.addstr((self.height - 1), 0,
                               ("%d peer%s connected (" % (self.torrent_details['peersConnected'],
                                                           ('s', '')[self.torrent_details['peersConnected'] == 1]) +
                                "Trackers:%d " % self.torrent_details['peersFrom']['fromTracker']
                                + "DHT:%d " % self.torrent_details['peersFrom']['fromDht']
                                + "LTEP:%d " % self.torrent_details['peersFrom']['fromLtep']
                                + "PEX:%d " % self.torrent_details['peersFrom']['fromPex']
                                + "Incoming:%d " % self.torrent_details['peersFrom']['fromIncoming']
                                + "Cache:%d)" % self.torrent_details['peersFrom']['fromCache'])[:self.width-1],
                               gconfig.element_attr('bottom_line'))
        elif self.vmode_id > -1:
            self.screen.addstr((self.height - 1), 0, "-- VISUAL --", gconfig.element_attr('bottom_line'))
        else:
            if self.narrow:
                strings = ['T', 'D', 'S', 'P', '', '!', ' ', ' S:%d', ' F:%d', 'Sz']
            else:
                strings = ["Torrent%s:" % ('s', '')[len(self.torrents) == 1], "Downloading:", "Seeding:", "Paused:",
                           "Filter:", "not ", " Sort by:", " Selected:%d", " Files:%d", 'Size:']
            self.screen.addstr((self.height - 1), 0, strings[0],
                               gconfig.element_attr('bottom_line'))
            self.screen.addstr("%d (" % len(self.torrents), gconfig.element_attr('bottom_line'))

            downloading = len([x for x in self.torrents if x['status'] == Transmission.STATUS_DOWNLOAD])
            seeding = len([x for x in self.torrents if x['status'] == Transmission.STATUS_SEED])
            paused = len([x for x in self.torrents if x['status'] in [Transmission.STATUS_STOPPED, Transmission.STATUS_CHECK_WAIT,
                                                                      Transmission.STATUS_CHECK, Transmission.STATUS_DOWNLOAD_WAIT, Transmission.STATUS_SEED_WAIT]])

            total_size = sum(x['sizeWhenDone'] for x  in self.torrents)
            total_done = percent(total_size, sum(x['haveValid'] for x  in self.torrents))

            if downloading > 0:
                self.screen.addstr(strings[1], gconfig.element_attr('bottom_line'))
                self.screen.addstr("%d " % downloading, gconfig.element_attr('bottom_line'))
            if seeding > 0:
                self.screen.addstr(strings[2], gconfig.element_attr('bottom_line'))
                self.screen.addstr("%d " % seeding, gconfig.element_attr('bottom_line'))
            if paused > 0:
                self.screen.addstr(strings[3], gconfig.element_attr('bottom_line'))
                self.screen.addstr("%d " % paused, gconfig.element_attr('bottom_line'))
            self.screen.addstr(strings[9] + scale_bytes(total_size), gconfig.element_attr('bottom_line'))
            if total_done < 100:
                self.screen.addstr("[%.2f%%]" % total_done, gconfig.element_attr('bottom_line'))
            self.screen.addstr(") ", gconfig.element_attr('bottom_line'))

            if self.selected_torrent == -1:
                if gconfig.filters[0][0]['name']:
                    self.screen.addstr(strings[4], gconfig.element_attr('bottom_line'))
                    if not self.narrow and gconfig.filters[0][0]['name'] in gconfig.FILTERS_WITH_PARAM:
                        filter_param = ('=', '!=')[gconfig.filters[0][0]['inverse']] + gconfig.filters[0][0][gconfig.filters[0][0]['name']][-16:]
                        not_str = ''
                    else:
                        filter_param = ''
                        not_str = ('', strings[5])[gconfig.filters[0][0]['inverse']]
                    safe_addstr(self.screen, not_str + gconfig.filters[0][0]['name'] + filter_param,
                                       gconfig.element_attr('filter_status' if len(gconfig.filters[0]) <= 1 else 'multi_filter_status')
                                       ^ (curses.A_REVERSE if self.filters_inverted else 0))

                # show last sort order (if terminal size permits it)
                if gconfig.sort_orders and self.width - self.screen.getyx()[1] > 20:
                    self.screen.addstr(strings[6], gconfig.element_attr('bottom_line'))
                    name = [name[1] for name in gconfig.sort_options if name[0] == gconfig.sort_orders[-1]['name']][0]
                    name = name.replace('_', '').lower()
                    curses_tags = gconfig.element_attr('sort_status')
                    if gconfig.sort_orders[-1]['reverse']:
                        self.screen.addch(curses.ACS_DARROW, curses_tags)
                    else:
                        self.screen.addch(curses.ACS_UARROW, curses_tags)
                    safe_addstr(self.screen, name, curses_tags)

                if self.selected and self.width - self.screen.getyx()[1] > 20:
                    self.screen.addstr(strings[7] % len(self.selected), gconfig.element_attr('bottom_line'))

            else:
                if self.details_category_focus == 1:
                    if gconfig.file_sort_key and self.width - self.screen.getyx()[1] > 20:
                        self.screen.addstr(strings[6], gconfig.element_attr('bottom_line'))
                        name = [name[1] for name in gconfig.file_sort_options if name[0] == gconfig.file_sort_key][0]
                        name = name.replace('_', '').lower()
                        curses_tags = gconfig.element_attr('filter_status')
                        if gconfig.file_sort_reverse:
                            self.screen.addch(curses.ACS_DARROW, curses_tags)
                        else:
                            self.screen.addch(curses.ACS_UARROW, curses_tags)
                        self.screen.addstr(name[:10], curses_tags)
                    if self.width - self.screen.getyx()[1] > 20 and len(self.torrent_details['files']) > 1:
                        self.screen.addstr(strings[8] % len(self.torrent_details['files']), gconfig.element_attr('bottom_line'))
                    if self.width - self.screen.getyx()[1] > 20 and self.selected_files:
                        self.screen.addstr(strings[7] % len(self.selected_files), gconfig.element_attr('bottom_line'))

    def draw_global_rates(self):
        # ↑1.2K ↓3.4M
        # ^    ^^     => +3
        rates_width = self.rateDownload_width + self.rateUpload_width + 3

        if self.stats['alt-speed-enabled']:
            upload_limit = "/%dK" % self.stats['alt-speed-up']
            download_limit = "/%dK" % self.stats['alt-speed-down']
        else:
            upload_limit = ('', "/%dK" % self.stats['speed-limit-up'])[self.stats['speed-limit-up-enabled']]
            download_limit = ('', "/%dK" % self.stats['speed-limit-down'])[self.stats['speed-limit-down-enabled']]

        limits = {'dn_limit': download_limit, 'up_limit': upload_limit}
        limits_width = len(limits['dn_limit']) + len(limits['up_limit'])

        if self.stats['alt-speed-enabled']:
            if self.narrow:
                self.screen.move(self.height - 1, self.width - rates_width - limits_width - 1)
                self.screen.addch(curses.ACS_TTEE, gconfig.element_attr('bottom_line') | curses.A_BOLD)
            else:
                self.screen.move(self.height - 1, self.width - rates_width - limits_width - len('Turtle mode '))
                self.screen.addstr('Turtle mode ', gconfig.element_attr('bottom_line') | curses.A_BOLD)

        self.screen.move(self.height - 1, self.width - rates_width - limits_width)
        self.screen.addch(curses.ACS_DARROW, gconfig.element_attr('bottom_line'))
        self.screen.addstr(scale_bytes(self.stats['downloadSpeed']).rjust(self.rateDownload_width)[:self.rateDownload_width],
                           gconfig.element_attr('download_rate'))
        self.screen.addstr(limits['dn_limit'], gconfig.element_attr('bottom_line'))
        self.screen.addch(' ', gconfig.element_attr('bottom_line'))
        self.screen.addch(curses.ACS_UARROW, gconfig.element_attr('bottom_line'))
        try:
            self.screen.addstr(scale_bytes(self.stats['uploadSpeed']).rjust(self.rateUpload_width)[:self.rateUpload_width],
                               gconfig.element_attr('upload_rate'))
            self.screen.addstr(limits['up_limit'], gconfig.element_attr('bottom_line'))
        except curses.error:
            # curses can print to the last char (bottom right corner), but it raises an exception.
            pass

    def draw_title_bar(self):
        self.screen.addstr(0, 0, ' '.center(self.width), gconfig.element_attr('top_line'))
        w = self.draw_connection_status()
        self.draw_quick_help(self.width - w - 2)

    def draw_connection_status(self):
        if self.narrow:
            status = "V.%s@%s:%s" % (self.server.version, gconfig.host, gconfig.port)
        else:
            status = "Transmission %s@%s:%s" % (self.server.version, gconfig.host, gconfig.port)
        self.screen.addstr(0, 0, status, gconfig.element_attr('top_line'))
        return len(status)

    def draw_quick_help(self, maxwidth):
        help_strings = [('?', 'Help')]

        if self.selected_torrent == -1:
            if self.focus >= 0:
                help_strings = [('enter', 'View'), ('p', 'Pause/Unpause'), ('r', 'Remove'), ('v', 'Verify')]
            else:
                help_strings = [('/', 'Search'), ('f', 'Filter'), ('s', 'Sort')] + help_strings + [('o', 'Options'), ('q', 'Quit')]
        else:
            help_strings = [('Move with', 'cursor keys'), ('q', 'Back to List')]
            if self.details_category_focus == 1 and self.focus_detaillist > -1:
                help_strings = [('enter', 'Open File'),
                                ('space', '(De)Select File'),
                                ('V', 'Visually Select Files'),
                                ('left/right', 'De-/Increase Priority'),
                                ('esc', 'Unfocus/-select')] + help_strings
            elif self.details_category_focus == 2:
                help_strings = [('F1/?', 'Explain flags')] + help_strings
            elif self.details_category_focus == 3:
                help_strings = [('a', 'Add Tracker'), ('r', 'Remove Tracker')] + help_strings

        # Greedy algorithm
        line = ''
        for x in help_strings:
            t = "%s:%s" % (x[0], x[1])
            if len(line) + len(t) + 1 <= maxwidth:
                line = line + ' ' + t
        self.screen.addstr(0, self.width - len(line), line, gconfig.element_attr('top_line'))

    def action_list_key_bindings(self):
        def key_name(k):
            map_key_names = {'UP': 'Up', 'DC': 'Del', 'SDC': 'Shift-Del', 'PPAGE': 'PgUp', 'NPAGE': 'PgDn'}
            if k in map_key_names:
                return map_key_names[k]
            if len(k) == 2 and k[0] == 1 and k[1].isdigit():
                return k[1]
            if len(k) == 2 and k[1] == '_':
                return '^'+k[0]
            return k.title() if len(k) > 2 else k

        title = 'Help Menu'
        if self.details_category_focus == 2:
            title = 'Peer status flags'
            message = " O  Optimistic unchoke\n" + \
                      " D  Downloading from this peer\n" + \
                      " d  We would download from this peer if they'd let us\n" + \
                      " U  Uploading to peer\n" + \
                      " u  We would upload to this peer if they'd ask\n" + \
                      " K  Peer has unchoked us, but we're not interested\n" + \
                      " ?  We unchoked this peer, but they're not interested\n" + \
                      " E  Encrypted Connection\n" + \
                      " H  Peer was discovered through DHT\n" + \
                      " X  Peer was discovered through Peer Exchange (PEX)\n" + \
                      " I  Peer is an incoming connection\n" + \
                      " T  Peer is connected via uTP"
        else:
            message = ''
            if self.selected_torrent == -1:
                categories = [0, 1]
            elif self.details_category_focus == 1:
                categories = [0, 2, 3]
            elif self.details_category_focus == 3:
                categories = [0, 2, 4]
            else:
                categories = [0, 2]

            movement_keys = True
            for a, d in gconfig.actions.items():
                if d[1] and d[0] & 15 in categories:
                    if d[0] & 256 and self.server.get_rpc_version() < 14:
                        continue
                    if d[0] & 512 and self.server.get_rpc_version() < 16:
                        continue
                    if d[0] & 1024 and self.server.get_rpc_version() < 17:
                        continue
                    if d[0] == 16 and movement_keys:
                        movement_keys = False
                        message += '           Movement Keys:\n'
                    if a == 'profile_menu':
                        message += "         0..9  Select profile\n"
                    if a == 'move_queue_down':
                        message += "Shft+Lft/Rght  Move focused torrent in queue up/down by 10\n"
                        message += "Shft+Home/End  Move focused torrent to top/bottom of queue\n"
                    keys_str = '/'.join(key_name(k) for k in d[1])[-13:].rjust(13)
                    message += keys_str + '  ' + d[2] + '\n'
        width = max([len(x) for x in message.split("\n")]) + 4
        width = min(self.width, width)
        height = min(self.height, message.count("\n") + 3)
        while True:
            win, last = self.help_window(height, width, message=message, title=title)
            while True:
                c = self.wingetch(win)
                if c in [K.SPACE, curses.KEY_NPAGE]:
                    win, last = self.help_window(height, width, message=message, title=title, first=last, win=win)
                elif c >= 0:
                    return
                self.update_torrent_list([win])

    def wingetch(self, win):
        c = win.getch()
        if c == K.W_:
            self.exit_now = True
        return c

    def win_message(self, win, height, width, message, first=0):
        ypos = 1
        lines = message.split("\n")
        pages = (len(lines) - 1) // (height - 2) + 1
        page = first // (height - 2) + 1
        for line in lines[first:]:
            if len_columns(line) > width - 3:
                line = ljust_columns(line, width - 6) + '...'

            if ypos < height - 1:  # ypos == height-1 is frame border
                win.addstr(ypos, 2, line)
                ypos += 1
            else:
                # Do not write outside of frame border
                win.addstr(height - 1, 2, "%d/%d" % (page, pages))
                return win, ypos + first - 1
            if pages > 1:
                win.addstr(height - 1, 2, "%d/%d" % (page, pages))
        return win, 0

    def window(self, height, width, message='', title='', xpos=None, attr='dialog'):
        return self.real_window(height, width, message=message, title=title, xpos=xpos, attr=attr)[0]

    def help_window(self, height, width, message='', title='', first=0, win=None):
        return self.real_window(height, width, message=message, title=title, first=first, win=win)

    def real_window(self, height, width, message='', title='', first=0, win=None, keypad=True, xpos=None, attr='dialog'):
        height = min(self.mainview_height, height)
        width = min(self.width, width)
        ypos = int((self.height - height) / 2)
        if xpos is None:
            xpos = int((self.width - width) / 2)
        if not win:
            win = curses.newwin(height, width, ypos, xpos)
            win.keypad(keypad)
        else:
            win.erase()
        win.box()
        win.bkgd(' ', gconfig.element_attr(attr))

        if width >= 20:
            win.addch(height - 1, width - 19, curses.ACS_RTEE)
            win.addstr(height - 1, width - 18, " Close with Esc ")
            win.addch(height - 1, width - 2, curses.ACS_LTEE)

        if width >= (len(title) + 6) and title != '':
            win.addch(0, 1, curses.ACS_RTEE)
            win.addstr(0, 2, " " + title + " ")
            win.addch(0, len(title) + 4, curses.ACS_LTEE)

        return self.win_message(win, height, width, message, first)

    def dialog_ok(self, message):
        height = 3 + message.count("\n")
        width = max(max([len_columns(x) for x in message.split("\n")]), 40) + 4
        win = self.window(height, width, message=message)
        while True:
            c = self.wingetch(win)
            if c in gconfig.esc_keys_w:
                return -1
            self.update_torrent_list([win])

    def dialog_yesno(self, message, important=False, hard=None):
        if hard:
            important = True
            message = message + "\n" + hard + "\n\n   Press ctrl-y to accept.\n"
        attr = 'dialog_important' if important else 'dialog'
        height = 5 + message.count("\n")
        width = max(len_columns(message), 8) + 4
        win = self.window(height, width, message=message, attr=attr)

        choice = False
        while True:
            win.move(int(height - 2), int(width / 2) - 4)
            if not hard:
                if choice:
                    bg = win.getbkgd()
                    win.bkgdset(gconfig.element_attr('menu_focused'))
                    win.addstr('Y', curses.A_UNDERLINE)
                    win.addstr('es')
                    win.bkgdset(bg)
                    win.addstr('   ')
                    win.addstr('N', curses.A_UNDERLINE)
                    win.addstr('o')
                else:
                    win.addstr('Y', curses.A_UNDERLINE)
                    win.addstr('es')
                    win.addstr('   ')
                    bg = win.getbkgd()
                    win.bkgdset(gconfig.element_attr('menu_focused'))
                    win.addstr('N', curses.A_UNDERLINE)
                    win.addstr('o')
                    win.bkgdset(bg)

            c = self.wingetch(win)
            if hard:
                if c == K.Y_:
                    return True
                if c in (K.n, K.LF, K.CR, curses.KEY_ENTER, K.SPACE):
                    return False
                if c in gconfig.esc_keys_w_no_ascii:
                    return 0
            else:
                if c == K.y:
                    return True
                if c == K.n:
                    return False
                if c == K.TAB:
                    choice = not choice
                elif c in (curses.KEY_LEFT, K.h):
                    choice = True
                elif c in (curses.KEY_RIGHT, K.l):
                    choice = False
                elif c in (K.LF, K.CR, curses.KEY_ENTER, K.SPACE):
                    return choice
                if c in gconfig.esc_keys_w_no_ascii:
                    return 0
            self.update_torrent_list([win])

    def dialog_input_text(self, message, text='', on_change=None, on_enter=None, tab_complete=None, maxwidth=9999,
                          align='center', history=None, history_max=0, fixed_history=[], search='', winstack=[]):
        """tab_complete values:
                'files': complete with any files/directories
                'dirs': complete only with directories
                'torrent_list': complete with names from the torrent list
                'executable': complete with executable name
                any false value: do not complete
        """
        path_executables=set()
        self.highlight_dialog = False
        if history is not None:
            localhistory = fixed_history + history + [text]
        else:
            localhistory = [text]
        history_pos = len(localhistory) - 1
        width = min(maxwidth, self.width - 4)
        textwidth = width - 4
        height = message.count("\n") + 4
        if align == 'center':
            xpos = None
        elif align == 'right':
            xpos = self.width - width

        win = self.window(height, width, message=message, xpos=xpos)
        show_cursor()
        if not isinstance(text, str):
            text = str(text, gconfig.ENCODING)
        index = len(text)
        initial_text = text
        tab_count = 0
        while True:
            # Cut the text into pages, each as long as the text field
            # The current page is determined by index position
            page = index // textwidth
            displaytext = text[textwidth * page:textwidth * (page + 1)]
            displayindex = index - textwidth * page

            color = gconfig.element_attr('dialog_text_important') if self.highlight_dialog \
                    else gconfig.element_attr('dialog_text')

            bg = win.getbkgd()
            win.bkgdset(0)
            win.addstr(height - 2, 2, displaytext.ljust(textwidth), color)
            win.bkgdset(bg)
            win.move(height - 2, displayindex + 2)
            c = self.wingetch(win)
            if history is not None:
                if c in (curses.KEY_UP, K.P_):
                    history_pos = (history_pos - 1) % len(localhistory)
                    text = localhistory[history_pos]
                    index = len(text)
                if c in (curses.KEY_DOWN, K.N_):
                    history_pos = (history_pos + 1) % len(localhistory)
                    text = localhistory[history_pos]
                    index = len(text)
            if c in gconfig.esc_keys_w_no_ascii:
                hide_cursor()
                return ''
            if c == K.X_:
                text = ''
                index = 0
            if index < len(text) and (c in (curses.KEY_RIGHT, K.F_)):
                index += 1
            elif index > 0 and (c in (curses.KEY_LEFT, K.B_)):
                index -= 1
            elif (c in (curses.KEY_BACKSPACE, K.DEL)) and index > 0:
                text = text[:index - 1] + (index < len(text) and text[index:] or '')
                index -= 1
                tab_count = 0
            elif index < len(text) and (c in (curses.KEY_DC, K.D_)):
                text = text[:index] + text[index + 1:]
            elif index < len(text) and c == K.K_:
                text = text[:index]
            elif c == K.U_:
                # Delete from cursor until beginning of line
                text = text[index:]
                index = 0
            elif c in (curses.KEY_HOME, K.A_):
                index = 0
            elif c in (curses.KEY_END, K.E_):
                index = len(text)
            elif c in (K.LF, K.CR, curses.KEY_ENTER, K.R_, K.T_):
                if history is not None and text != '':
                    try:
                        p = history.index(text)
                        history.pop(p)
                    except Exception:
                        p = -1
                    if len(history) >= history_max:
                        history.pop(0)
                    history.append(text)
                if on_enter:
                    if c in (K.LF, K.CR, curses.KEY_ENTER):
                        inc = 1
                    elif c == K.R_:
                        inc = -1
                    else:
                        inc = 0
                    if on_enter(text, inc=inc, search=search):
                        hide_cursor()
                        return None
                else:
                    hide_cursor()
                    return text
            elif 32 <= c < 127:
                text = text[:index] + chr(c) + (index < len(text) and text[index:] or '')
                index += 1
            elif c == K.TAB and tab_complete:
                if tab_count == 0:
                    initial_text = text
                else:
                    text = initial_text
                possible_choices = []
                if tab_complete in ('files', 'dirs'):
                    (dirname, filename) = os.path.split(tilde2homedir(text))
                    if not dirname:
                        dirname = str(os.getcwd())
                    try:
                        possible_choices = [os.path.join(dirname, choice) for choice in os.listdir(dirname)
                                            if choice.startswith(filename)]
                        possible_choices.sort()
                    except OSError:
                        continue
                    if tab_complete == 'dirs':
                        possible_choices = [d for d in possible_choices
                                            if os.path.isdir(d)]
                elif tab_complete == 'torrent_list':
                    possible_choices = [t['name'] for t in self.torrents
                                        if t['name'].startswith(text)]
                elif tab_complete == 'file_list':
                    possible_choices = [f for f in [os.path.basename(g['name']) for g in self.sorted_files]
                                        if f.startswith(text)]
                elif tab_complete == 'executable':
                    if not path_executables:
                        paths = os.environ["PATH"].split(":")
                        for p in paths:
                            if os.path.isdir(p):
                                path_executables.update(os.listdir(p))
                    possible_choices = list(p for p in path_executables if p.startswith(text))
                if possible_choices:
                    text = os.path.commonprefix(possible_choices)
                    if tab_complete in ('files', 'dirs'):
                        num_possible_choices = len(possible_choices)
                        if num_possible_choices == 1 and os.path.isdir(text) and not text.endswith(os.sep):
                            text += os.sep
                        elif tab_count <= num_possible_choices:
                            if tab_count == num_possible_choices:
                                tab_count = 0
                            text = possible_choices[tab_count]
                            tab_count += 1
                        text = homedir2tilde(text)
                    index = len(text)
            if on_change:
                if localhistory[-1] != text:
                    on_change(text)
            if localhistory[-1] != text and text not in localhistory:
                localhistory[-1] = text
            self.update_torrent_list(winstack + [win], pattern=text, search=search)

    def action_search_torrent(self):
        self.dialog_input_text('Search torrent by title:',
                               on_enter=self.increment_search,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='pattern')

    def action_search_torrent_fulltext(self):
        self.dialog_input_text('Search torrent by title (full text):',
                               on_enter=self.increment_search,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='fulltext')

    def action_search_torrent_regex(self):
        self.dialog_input_text('Regex search torrent by title:',
                               on_enter=self.increment_search,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='regex')

    def action_search_torrent_regex_fulltext(self):
        self.dialog_input_text('Regex search torrent by title (full text):',
                               on_enter=self.increment_search,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='regex_fulltext')

    def action_search_file(self):
        self.dialog_input_text('Search file by title:',
                               on_enter=self.increment_file_search,
                               tab_complete='file_list',
                               maxwidth=60, align='right', search='pattern')

    def action_search_file_regex(self):
        self.dialog_input_text('Regex search file by title:',
                               on_enter=self.increment_file_search,
                               tab_complete='file_list',
                               maxwidth=60, align='right', search='regex')

    def increment_file_search(self, pattern, inc=1, search=None):
        self.search_focus += inc

    def increment_search(self, pattern, inc=1, search=None):
        self.search_focus += inc

    def action_select_search_torrent(self):
        self.dialog_input_text('Select torrents matching pattern',
                               on_enter=self.select_pattern_torrents,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='pattern')

    def action_select_search_torrent_fulltext(self):
        self.server.set_torrent_details_id([t['id'] for t in self.torrents])
        self.server.wait_for_details_update()
        self.server.set_torrent_details_id(-1)
        self.dialog_input_text('Select torrents matching pattern (full text)',
                               on_enter=self.select_pattern_torrents,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='fulltext')

    def action_select_search_torrent_regex(self):
        self.dialog_input_text('Select torrents matching regex',
                               on_enter=self.select_pattern_torrents,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='regex')

    def action_select_search_torrent_regex_fulltext(self):
        self.dialog_input_text('Select torrents matching regex (full text)',
                               on_enter=self.select_pattern_torrents,
                               tab_complete='torrent_list',
                               maxwidth=60, align='right', search='regex_fulltext')

    def action_select_search_file(self):
        self.dialog_input_text('Select files matching pattern',
                               on_enter=self.select_pattern_files,
                               tab_complete='file_list',
                               maxwidth=60, align='right', search='pattern')

    def action_select_search_file_regex(self):
        self.dialog_input_text('Select files matching regex',
                               on_enter=self.select_pattern_files,
                               tab_complete='file_list',
                               maxwidth=60, align='right', search='regex')

    def select_pattern_torrents(self, pattern, inc=1, search=None):
        if search in ['fulltext', 'regex_fulltext']:
            torrents_files = self.get_torrents_filenames()
        else:
            torrents_files = None
        if search in ['pattern', 'fulltext']:
            matched_torrents = {t['id'] for t in self.torrents if pattern.lower() in self.torrent_text(t, search, torrents_files)}
        elif search in ['regex', 'regex_fulltext']:
            try:
                regex = re.compile(pattern, re.I)
                matched_torrents = {t['id'] for t in self.torrents if regex.search(self.torrent_text(t, search, torrents_files))}
            except Exception:
                return True
        else:
            return True
        if inc == 1:
            self.selected = matched_torrents
        elif inc == 0:
            self.selected.intersection_update(matched_torrents)
        elif inc == -1:
            self.selected.update(matched_torrents)
        return True

    def select_pattern_files(self, pattern, inc=1, search=None):
        if search == 'pattern':
            matched_files = [i for i in range(len(self.sorted_files)) if pattern.lower() in os.path.basename(self.sorted_files[i]['name'].lower())]
        elif search == 'regex':
            try:
                regex = re.compile(pattern, re.I)
                matched_files = [i for i in range(len(self.sorted_files)) if regex.search(os.path.basename(self.sorted_files[i]['name']))]
            except Exception:
                return True
        else:
            return True
        if inc == 1:
            self.selected_files = matched_files
        elif inc == 0:
            self.selected_files = [f for f in self.selected_files if f in matched_files]
        elif inc == -1:
            self.selected_files = list(set(self.selected_files).union(set(matched_files)))
        return True

    def dialog_input_number(self, message, current_value,
                            floating_point=False, allow_empty=False,
                            allow_zero=True, allow_negative_one=True, winstack=[]):
        if not allow_zero:
            allow_negative_one = False

        width = max(max([len(x) for x in message.split("\n")]), 40) + 4
        width = min(self.width, width)
        height = message.count("\n") + 6

        show_cursor()
        win = self.window(height, width, message=message)
        value = str(current_value)
        if floating_point:
            bigstep = 1
            smallstep = 0.1
        else:
            bigstep = 100
            smallstep = 10
        win.addstr(height - 4, 2, ("   up/down +/- %-3s" % bigstep).rjust(width - 4))
        win.addstr(height - 3, 2, ("left/right +/- %3s" % smallstep).rjust(width - 4))
        if allow_negative_one:
            win.addstr(height - 3, 2, "-1 means unlimited")
        if allow_empty:
            win.addstr(height - 4, 2, "leave empty for default")

        while True:
            bg = win.getbkgd()
            win.bkgdset(0)
            win.addstr(height - 2, 2, value.ljust(width - 4), gconfig.element_attr('dialog_text'))
            win.bkgdset(bg)
            win.move(height - 2, len(value) + 2)
            c = self.wingetch(win)
            if c in gconfig.esc_keys_w:
                hide_cursor()
                return -128
            if c in (K.LF, K.CR, curses.KEY_ENTER):
                hide_cursor()
                try:
                    if allow_empty and len(value) <= 0:
                        return -2
                    if floating_point:
                        return float(value)
                    return int(value)
                except ValueError:
                    return -1

            elif c in (curses.KEY_BACKSPACE, curses.KEY_DC, K.DEL, 8):
                value = value[:-1]
            elif c in (K.U_, K.X_):
                value = ''
            elif len(value) >= width - 5:
                curses.beep()
            elif K.n1 <= c <= K.n9:
                value += chr(c)
            elif allow_zero and c == K.n0 and value != '-' and not value.startswith('0'):
                value += chr(c)
            elif allow_negative_one and c == K.MINUS and len(value) == 0:
                value += chr(c)
            elif floating_point and c == K.DOT and '.' not in value:
                value += chr(c)

            elif c != -1:
                try:
                    if value == '':
                        value = 0
                    number = float(value) if floating_point else int(value)
                    if c in (curses.KEY_LEFT, K.h):
                        number -= smallstep
                    elif c in (curses.KEY_RIGHT, K.l):
                        number += smallstep
                    elif c in (curses.KEY_DOWN, K.j):
                        number -= bigstep
                    elif c in (curses.KEY_UP, K.k):
                        number += bigstep
                    if not allow_zero and number <= 0:
                        number = 1
                    elif not allow_negative_one and number < 0:
                        number = 0
                    elif number < 0:  # value like -0.6 isn't useful
                        number = -1
                    value = ("%.2f" % number).rstrip('0').rstrip('.') if floating_point else str(number)
                except ValueError:
                    pass
            self.update_torrent_list(winstack + [win])

    def dialog_menu(self, title, options, focus=1, extended=False, winstack=[]):
        height = len(options) + 2
        paging = False
        if self.mainview_height < height:
            height = self.mainview_height
            paging = True
        pagelines = height - 2
        width = max(max([len(x[1]) + 3 for x in options]), len(title) + 3)
        win = self.window(height, width)

        win.addstr(0, 1, title)
        if paging:
            if width > 35:
                win.addstr(height - 1, 1, "More...")
            else:
                win.addstr(height - 1, 1, "+")

        old_page = 0
        while True:
            page = (focus - 1) // pagelines
            if page < 0:
                page = 0
            if page != old_page:
                for i in range(1, height - 1):
                    win.addstr(i, 2, ' ' * (width - 4), 0)
            keymap = self.dialog_list_menu_options(win, width, options, focus, page * pagelines, (page + 1) * pagelines)
            c = self.wingetch(win)

            if 47 < c < 123 and chr(c).lower() in keymap:
                return (options[keymap[chr(c).lower()]][0], chr(c).isupper(), win) if extended else options[keymap[chr(c).lower()]][0]
            if c in gconfig.esc_keys_w:
                return (-128, False, win) if extended else -128
            if c in (K.LF, K.CR, curses.KEY_ENTER):
                return (options[focus - 1][0], False, win) if extended else options[focus - 1][0]
            if c == curses.KEY_BACKSPACE and extended:
                return (options[focus - 1][0], True, win)
            if c in (curses.KEY_DOWN, K.j, K.N_):
                focus += 1
                if focus > len(options):
                    focus = 1
            elif c in (curses.KEY_UP, K.k, K.P_):
                focus -= 1
                if focus < 1:
                    focus = len(options)
            elif c in (curses.KEY_HOME, K.g):
                focus = 1
            elif c in (curses.KEY_END, K.G):
                focus = len(options)
            elif c == -1:
                self.update_torrent_list(winstack + [win])

    def dialog_list_menu_options(self, win, width, options, focus, startline, endline):
        keys = dict()
        i = 1
        for option in options:
            title = option[1].split('_', 1)
            if startline < i <= endline:
                if i == focus:
                    bg = win.getbkgd()
                    win.bkgdset(gconfig.element_attr('menu_focused'))
                win.addstr(i - startline, 2, title[0])
                if len(title) > 1:
                    win.addstr(title[1][0], curses.A_UNDERLINE)
                    win.addstr(title[1][1:])
                    keys[title[1][0].lower()] = i - 1
                win.addstr(''.ljust(width - len(option[1]) - 3))
                if i == focus:
                    win.bkgdset(bg)
            i += 1
        return keys

    def action_server_options_dialog(self):
        enc_options = [('required', '_required'), ('preferred', '_preferred'), ('tolerated', '_tolerated')]
        first_time = True
        while True:
            options = []
            options.append(('Peer _Port', "%d" % self.stats['peer-port']))
            options.append(('UP_nP/NAT-PMP', ('disabled', 'enabled ')[self.stats['port-forwarding-enabled']]))
            options.append(('Peer E_xchange', ('disabled', 'enabled ')[self.stats['pex-enabled']]))
            options.append(('_Distributed Hash Table', ('disabled', 'enabled ')[self.stats['dht-enabled']]))
            options.append(('_Local Peer Discovery', ('disabled', 'enabled ')[self.stats['lpd-enabled']]))
            options.append(('Protocol En_cryption', "%s" % self.stats['encryption']))
            # uTP support was added in Transmission v2.3
            if self.server.get_rpc_version() >= 13:
                options.append(('_Micro Transport Protocol', ('disabled', 'enabled')[self.stats['utp-enabled']]))
            options.append(('_Global Peer Limit', "%d" % self.stats['peer-limit-global']))
            options.append(('Peer Limit per _Torrent', "%d" % self.stats['peer-limit-per-torrent']))
            options.append(('Turtle m_ode', ('disabled', 'enabled ')[self.stats['alt-speed-enabled']]))
            options.append(('T_urtle Mode UL Limit', "%dK" % self.stats['alt-speed-up']))
            options.append(('Tu_rtle Mode DL Limit', "%dK" % self.stats['alt-speed-down']))
            options.append(('_Seed Ratio Limit', "%s" % ('unlimited', self.stats['seedRatioLimit'])[self.stats['seedRatioLimited']]))
            # queue was implemented in Transmission v2.4
            if self.server.get_rpc_version() >= 14:
                options.append(('Do_wnload Queue Size', "%s" % ('disabled', self.stats['download-queue-size'])[self.stats['download-queue-enabled']]))
                options.append(('S_eed Queue Size', "%s" % ('disabled', self.stats['seed-queue-size'])[self.stats['seed-queue-enabled']]))

            if first_time:
                first_time = False
                max_len = max([sum([len(re.sub('_', '', x)) for x in y[0]]) for y in options])
                width = min(max(len(gconfig.file_viewer) + 6, 15) + max_len, self.width)
                height = len(options) + 2
                paging = False
                if self.mainview_height < height:
                    height = self.mainview_height
                    paging = True
                pagelines = height - 2
                page = 0
                old_page = -1
                win = self.window(height, width, '', "Server Options")
                if paging:
                    if width > 35:
                        win.addstr(height - 1, 1, "More...")
                    else:
                        win.addstr(height - 1, 1, "+")

            for i in range(1, height - 1):
                win.addstr(i, 2, ' ' * (width - 3), 0)
            linestart, lineend = page * pagelines, (page + 1) * pagelines
            line_num = 1
            for option in options:
                parts = re.split('_', option[0])
                parts_len = sum([len(x) for x in parts])

                if linestart < line_num <= lineend:
                    win.addstr(line_num - linestart, max_len - parts_len + 2, parts.pop(0))
                    for part in parts:
                        win.addstr(part[0], curses.A_UNDERLINE)
                        win.addstr(part[1:] + ': ' + option[1])
                line_num += 1

            key = self.wingetch(win)
            if key in gconfig.esc_keys_w_enter:
                return
            if key == K.p:
                port = self.dialog_input_number("Port for incoming connections",
                                                self.stats['peer-port'],
                                                allow_negative_one=False, winstack=[win])
                if 0 <= port <= 65535:
                    self.server.set_option('peer-port', port)
                elif port != -128:  # user hit ESC
                    self.dialog_ok('Port must be in the range of 0 - 65535')
            elif key == K.n:
                self.server.set_option('port-forwarding-enabled',
                                       (1, 0)[self.stats['port-forwarding-enabled']])
            elif key == K.x:
                self.server.set_option('pex-enabled', (1, 0)[self.stats['pex-enabled']])
            elif key == K.d:
                self.server.set_option('dht-enabled', (1, 0)[self.stats['dht-enabled']])
            elif key == K.l:
                self.server.set_option('lpd-enabled', (1, 0)[self.stats['lpd-enabled']])
            # uTP support was added in Transmission v2.3
            elif key == K.m and self.server.get_rpc_version() >= 13:
                self.server.set_option('utp-enabled', (1, 0)[self.stats['utp-enabled']])
            elif key == K.g:
                limit = self.dialog_input_number("Maximum number of connected peers",
                                                 self.stats['peer-limit-global'],
                                                 allow_negative_one=False, winstack=[win])
                if limit >= 0:
                    self.server.set_option('peer-limit-global', limit)
            elif key == K.t:
                limit = self.dialog_input_number("Maximum number of connected peers per torrent",
                                                 self.stats['peer-limit-per-torrent'],
                                                 allow_negative_one=False, winstack=[win])
                if limit >= 0:
                    self.server.set_option('peer-limit-per-torrent', limit)
            elif key == K.s:
                limit = self.dialog_input_number('Stop seeding with upload/download ratio',
                                                 (-1, self.stats['seedRatioLimit'])[self.stats['seedRatioLimited']],
                                                 floating_point=True, winstack=[win])
                if limit >= 0:
                    self.server.set_option('seedRatioLimit', limit)
                    self.server.set_option('seedRatioLimited', True)
                elif limit < 0 and limit != -128:
                    self.server.set_option('seedRatioLimited', False)
            elif key == K.c:
                choice = self.dialog_menu('Encryption', enc_options,
                                          list(map(lambda x: x[0] == self.stats['encryption'], enc_options)).index(True) + 1, winstack=[win])
                if choice != -128:
                    self.server.set_option('encryption', choice)
            elif key == K.o:
                self.server.toggle_turtle_mode()
            elif key == K.u:
                limit = self.dialog_input_number('Upload limit for Turtle Mode in kilobytes per second',
                                                 self.stats['alt-speed-up'],
                                                 allow_negative_one=False, winstack=[win])
                if limit != -128:
                    self.server.set_option('alt-speed-up', limit)
            elif key == K.r:
                limit = self.dialog_input_number('Download limit for Turtle Mode in kilobytes per second',
                                                 self.stats['alt-speed-down'],
                                                 allow_negative_one=False, winstack=[win])
                if limit != -128:
                    self.server.set_option('alt-speed-down', limit)
            # Queue was implemmented in Transmission v2.4
            elif key == K.w and self.server.get_rpc_version() >= 14:
                queue_size = self.dialog_input_number('Download Queue size',
                                                      (0, self.stats['download-queue-size'])[self.stats['download-queue-enabled']],
                                                      allow_negative_one=False, winstack=[win])
                if queue_size != -128:
                    if queue_size == 0:
                        self.server.set_option('download-queue-enabled', False)
                    elif queue_size > 0:
                        if not self.stats['download-queue-enabled']:
                            self.server.set_option('download-queue-enabled', True)
                        self.server.set_option('download-queue-size', queue_size)
            elif key == K.e and self.server.get_rpc_version() >= 14:
                queue_size = self.dialog_input_number('Seed Queue size',
                                                      (0, self.stats['seed-queue-size'])[self.stats['seed-queue-enabled']],
                                                      allow_negative_one=False, winstack=[win])
                if queue_size != -128:
                    if queue_size == 0:
                        self.server.set_option('seed-queue-enabled', False)
                    elif queue_size > 0:
                        if not self.stats['seed-queue-enabled']:
                            self.server.set_option('seed-queue-enabled', True)
                        self.server.set_option('seed-queue-size', queue_size)
            elif key == K.SPACE:
                page = page + 1
                if page > (len(options) - 1) / pagelines:
                    page = 0

            self.update_torrent_list([win])

    def action_options_dialog(self):
        first_time = True
        while True:
            options = []
            options.append(('Version', gconfig.VERSION))
            options.append(('Terminal size', "%d x %d " % (self.width, self.height)))
            options.append(('Title is Progress _Bar', ('no', 'yes')[gconfig.torrentname_is_progressbar]))
            options.append(('File _Viewer', "%s" % gconfig.file_viewer))
            options.append(("View _files", ('focused', 'selected')[gconfig.view_selected]))
            options.append(("Show peers' _reverse DNS", ('no', 'yes')[gconfig.rdns]))
            options.append(("Show torrent _numbers", ('no', 'yes')[gconfig.torrent_numbers]))
            options.append(("_Display format", ('wide', 'narrow')[self.narrow]))

            if first_time:
                first_time = False
                max_len = max([sum([len(re.sub('_', '', x)) for x in y[0]]) for y in options])
                width = min(max(len(gconfig.file_viewer) + 6, 15) + max_len, self.width)
                height = len(options) + 2
                paging = False
                if self.mainview_height < height:
                    height = self.mainview_height
                    paging = True
                pagelines = height - 2
                page = 0
                old_page = -1
                win = self.window(height, width, '', "Global Options")
                if paging:
                    if width > 35:
                        win.addstr(height - 1, 1, "More...")
                    else:
                        win.addstr(height - 1, 1, "+")

            for i in range(1, height - 1):
                win.addstr(i, 2, ' ' * (width - 3), 0)
            linestart, lineend = page * pagelines, (page + 1) * pagelines
            line_num = 1
            for option in options:
                parts = re.split('_', option[0])
                parts_len = sum([len(x) for x in parts])

                if linestart < line_num <= lineend:
                    win.addstr(line_num - linestart, max_len - parts_len + 2, parts.pop(0))
                    if parts:
                        win.addstr(parts[0][0], curses.A_UNDERLINE)
                        win.addstr(parts[0][1:])
                    win.addstr(': ' + option[1])
                line_num += 1

            key = self.wingetch(win)
            if key in gconfig.esc_keys_w_enter:
                return
            if key == K.b:
                gconfig.torrentname_is_progressbar = not gconfig.torrentname_is_progressbar
            elif key == K.d:
                self.force_narrow = not self.narrow
            elif key == K.f:
                gconfig.view_selected = not gconfig.view_selected
            elif key == K.r:
                gconfig.rdns = not gconfig.rdns
            elif key == K.n:
                gconfig.torrent_numbers = not gconfig.torrent_numbers
            elif key == K.v:
                viewer = self.dialog_input_text('File Viewer\nExample: xdg-viewer %s', gconfig.file_viewer,
                                                tab_complete='executable', winstack=[win])
                if viewer:
                    config.set('Misc', 'file_viewer', viewer.replace('%s', '%%s'))
                    gconfig.file_viewer = viewer
            elif key == K.SPACE:
                page = page + 1
                if page > (len(options) - 1) / pagelines:
                    page = 0
            self.update_torrent_list([win])

    def dialog_filters(self):
        filters = [[f.copy() for f in l] for l in gconfig.filters]
        filters.append([])
        changed = True
        current = [0, 0]
        oldheight, oldwidth = -1, -1
        needupdate = False
        while True:
            if changed:
                changed = False
                lines = []
                i = 0
                for fl in filters:
                    lines.append(', '.join([filter2string(f) for f in fl]) + ', ')
                    if lines[-1] == ', ':
                        lines[-1] = ''
                        if current[0] == len(lines) - 1:
                            current[1] = 0
                    if i == current[0]:
                        commas = [i for i, v in enumerate(lines[-1]) if v == ',']
                        commas.append(len(lines[-1]) + 1)
                        commas.insert(0, -2)
                    i += 1

                height = len(lines) + 3
                width = min(max([14] + [len(s) for s in lines]) + 6, self.width - 2, )
                if height > oldheight or width > oldwidth or win is None:
                    win = self.window(height, width, title='Filters')
                    oldheight, oldwidth = height, width
                y = 1
                for s in lines:
                    win.addnstr(y, 2, s, width - 4)
                    win.addstr(" " * (oldwidth - 3 - win.getyx()[1]))
                    y += 1
                win.chgat(1 + current[0], commas[current[1]] + 4, commas[current[1] + 1] - commas[current[1]] - 2, curses.A_UNDERLINE)
            c = self.wingetch(win)
            if c in gconfig.esc_keys_w:
                return gconfig.filters
            if c in (K.LF, K.CR, curses.KEY_ENTER):
                filters = [f for f in filters if f != []]
                return [[{'name': '', 'inverse': False}]] if filters == [] else filters
            if c == curses.KEY_UP and current[0] > 0:
                current[0] -= 1
                changed = True
            if c == curses.KEY_DOWN and current[0] < len(filters) - 1:
                current[0] += 1
                changed = True
            if c == curses.KEY_RIGHT and current[1] < len(filters[current[0]]):
                current[1] += 1
                changed = True
            if c == curses.KEY_LEFT and current[1] > 0:
                current[1] -= 1
                changed = True
            if c in (K.d, curses.KEY_DC) and current[1] < len(filters[current[0]]):
                filters[current[0]].pop(current[1])
                changed = True
            if c == K.f:
                f = filters[current[0]][current[1]].copy() if current[1] < len(filters[current[0]]) else {'name': '', 'inverse': False}
                f = self.filter_menu(oldfilter=f, winstack=[win])
                if f:
                    if current[1] < len(filters[current[0]]):
                        filters[current[0]][current[1]] = f
                    else:
                        filters[current[0]].append(f)
                    if current[0] == len(filters) - 1:
                        filters.append([])
                    changed = True
                needupdate = True

            if current[1] > len(filters[current[0]]):
                current[1] = len(filters[current[0]])

            if c == -1 or needupdate:
                needupdate = False
                self.update_torrent_list([win])

    def update_torrent_list(self, winstack=[], pattern='', search=''):
        self.server.update(1)
        self.draw_stats()
        if self.selected_torrent == -1:
            self.draw_torrent_list(search_keyword=pattern, search=search, refresh=False)
        else:
            self.draw_details(search_keyword=pattern, search=search, refresh=False)
        self.pad.noutrefresh(0, 0, 1, 0, self.mainview_height, self.width - 1)
        self.screen.noutrefresh()
        for win in winstack[:-1]:
            win.redrawwin()
            win.refresh()
        winstack[-1].redrawwin()

# End of class Interface


def load_history(filename):
    if filename:
        try:
            history = json.load(open(filename, "r"))
            assert isinstance(history, dict)
        except Exception:
            history = {}
    else:
        history = {}
    for i in ['label', 'location', 'tracker', 'command']:
        if i not in history:
            history[i] = []
    if 'types' not in history:
        history['types'] = {}
    return history


def save_history(filename, history):
    if filename:
        try:
            oldhistory = json.load(open(filename, "r"))
        except Exception:
            oldhistory = {}
        if oldhistory != history:
            try:
                json.dump(history, open(filename, "w"))
            except Exception:
                pass


def reverse_dns(cache, address):
    try:
        cache[address] = socket.gethostbyaddr(address)[0]
    except Exception:
        cache[address] = '<not resolvable>'


def percent(full, part):
    try:
        percent = 100 / (float(full) / float(part))
    except ZeroDivisionError:
        percent = 0.0
    return percent


def scale_time(seconds, long=False):
    minute_in_sec = float(60)
    hour_in_sec = float(3600)
    day_in_sec = float(86400)
    month_in_sec = 27.321661 * day_in_sec  # from wikipedia
    year_in_sec = 365.25 * day_in_sec  # from wikipedia

    if seconds < 0:
        return ('?', 'some time')[long]

    if seconds < minute_in_sec:
        if long:
            return 'now' if seconds < 5 else "%d second%s" % (seconds, ('', 's')[seconds > 1])
        return "%ds" % seconds

    if seconds < hour_in_sec:
        minutes = round(seconds / minute_in_sec, 0)
        if long:
            return "%d minute%s" % (minutes, ('', 's')[minutes > 1])
        return "%dm" % minutes

    if seconds < day_in_sec:
        hours = round(seconds / hour_in_sec, 0)
        if long:
            return "%d hour%s" % (hours, ('', 's')[hours > 1])
        return "%dh" % hours

    if seconds < month_in_sec:
        days = round(seconds / day_in_sec, 0)
        if long:
            return "%d day%s" % (days, ('', 's')[days > 1])
        return "%dd" % days

    if seconds < year_in_sec:
        months = round(seconds / month_in_sec, 0)
        if long:
            return "%d month%s" % (months, ('', 's')[months > 1])
        return "%dM" % months

    years = round(seconds / year_in_sec, 0)
    if long:
        return "%d year%s" % (years, ('', 's')[years > 1])
    return "%dy" % years


def timestamp(timestamp, time_format="%x %X", narrow=False):
    if timestamp < 1:
        return 'never'

    if timestamp > 2147483647:  # Max value of 32bit signed integer (2^31-1)
        # Timedelta objects do not fail on timestamps
        # resulting in a date later than 2038
        try:
            date = (datetime.datetime.fromtimestamp(0) +
                    datetime.timedelta(seconds=timestamp))
        except OverflowError:
            return 'some day in the distant future'
        date = (datetime.datetime.fromtimestamp(0) +
                datetime.timedelta(seconds=timestamp))
        timeobj = date.timetuple()
    else:
        timeobj = time.localtime(timestamp)

    if time_format == "%X" and (timestamp - time.time() < -86400 or timestamp - time.time() > 86400):
        time_format = "%x"
    absolute = time.strftime(time_format, timeobj)
    if narrow:
        if timestamp > time.time():
            relative = '+' + scale_time(int(timestamp - time.time()), not narrow)
        else:
            relative = '-' + scale_time(int(time.time() - timestamp), not narrow)
    else:
        if timestamp > time.time():
            relative = 'in ' + scale_time(int(timestamp - time.time()), True)
        else:
            relative = scale_time(int(time.time() - timestamp), True) + ' ago'

    if relative.startswith('now') or relative.endswith('now'):
        relative = 'now'
    return "%s (%s)" % (absolute, relative)


def scale_bytes(num=0, long=False):
    if num >= 1099511627776:
        scaled_num = round((num / 1099511627776.0), 1)
        unit = 'T'
    elif num >= 1073741824:
        scaled_num = round((num / 1073741824.0), 1)
        unit = 'G'
    elif num >= 1048576:
        scaled_num = round((num / 1048576.0), 1)
        unit = 'M'
    else:
        scaled_num = round((num / 1024.0), 1)
        unit = 'K'

    # handle 0 num special
    if num == 0 and long:
        return 'nothing'
    return num2str(num) + ' [' + num2str(scaled_num) + unit + ']' if long else str(scaled_num) + unit


def homedir2tilde(path):
    return re.sub(r'^' + os.environ['HOME'], '~', path)


def tilde2homedir(path):
    return re.sub(r'^~', os.environ['HOME'], path)


def html2text(s):
    s = re.sub(r'</h\d+>', "\n", s)
    s = re.sub(r'</p>', ' ', s)
    s = re.sub(r'<[^>]*?>', '', s)
    return s


def hide_cursor():
    try:
        curses.curs_set(0)   # hide cursor if possible
    except curses.error:
        pass  # some terminals seem to have problems with that


def show_cursor():
    try:
        curses.curs_set(1)
    except curses.error:
        pass


def safe_addstr(win, string, attr):
    win.addstr(string[:win.getmaxyx()[1] - win.getyx()[1] - 1], attr)


def wrap_multiline(text, width, initial_indent='', subsequent_indent=' '):
    if subsequent_indent is None:
        subsequent_indent = ' ' * len(initial_indent)
    for line in text.splitlines():
        # this is required because wrap() strips empty lines
        if not line.strip():
            yield line
            continue
        for line in wrap(line, width, replace_whitespace=False,
                         initial_indent=initial_indent, subsequent_indent=subsequent_indent):
            yield line
        initial_indent = subsequent_indent


def ljust_columns(text, max_width, padchar=' '):
    """ Returns a string that is exactly <max_width> display columns wide,
    padded with <padchar> if necessary. Accounts for characters that are
    displayed two columns wide, i.e. kanji. """

    chars = []
    columns = 0
    max_width = max(0, max_width)
    for character in text:
        width = len_columns(character)
        if columns + width <= max_width:
            chars.append(character)
            columns += width
        else:
            break

    # Fill up any remaining space
    while columns < max_width:
        assert len(padchar) == 1
        chars.append(padchar)
        columns += 1
    return ''.join(chars)


def len_columns(text):
    """ Returns the amount of columns that <text> would occupy. """
    columns = 0
    ret = 0
    for character in text:
        if character in ['\n']:
            columns = 0
        columns += 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
        if columns > ret:
            ret = columns
    return ret


def num2str(num, num_format='%s'):
    if int(num) == -1:
        return '?'
    if int(num) == -2:
        return 'oo'
    if num > 999:
        return (re.sub(r'(\d{3})', r'\g<1>,', str(num)[::-1])[::-1]).lstrip(',')
    return num_format % num


lastexitcode = -1
def exit_prog(msg='', exitcode=0):
    global lastexitcode
    try:
        curses.endwin()
    except curses.error:
        pass
    if msg or exitcode:
        print(msg, file=sys.stderr)
    if lastexitcode == -1:
        lastexitcode = exitcode
    elif exitcode == 0:
        exitcode = lastexitcode
    sys.exit(exitcode)


def read_netrc(file=os.environ['HOME'] + '/.netrc', hostname=None):
    try:
        login = password = ''
        try:
            login, _, password = netrc.netrc(file).authenticators(hostname)
        except TypeError:
            pass
        try:
            netrc.netrc(file).hosts[hostname]
        except KeyError:
            if hostname != 'localhost':
                pdebug("Unknown machine in %s: %s" % (file, hostname))
                if login and password:
                    pdebug("Using default login: %s" % login)
                else:
                    sys.exit(gconfig.errors.CONFIGFILE_ERROR)
    except netrc.NetrcParseError as e:
        exit_prog("Error in %s at line %s: %s\n" % (e.filename, e.lineno, e.msg))
    except IOError as msg:
        exit_prog("Cannot read %s: %s\n" % (file, msg))
    return login, password


# create initial config file
def create_config(configfile, connection):
    # create directory if necessary
    config_dir = os.path.dirname(configfile)
    if config_dir != '' and not os.path.isdir(config_dir):
        try:
            os.makedirs(config_dir)
        except OSError as msg:
            print(msg)
            sys.exit(gconfig.errors.CONFIGFILE_ERROR)

    # write file
    if not save_config(configfile, force=True):
        sys.exit(gconfig.errors.CONFIGFILE_ERROR)
    print("Wrote config file: %s" % configfile)
    sys.exit(0)


def save_config(filepath, force=False):
    if force or os.path.isfile(filepath):
        try:
            config.write(open(filepath, 'w'))
            os.chmod(filepath, 0o600)  # config may contain password
            return 1
        except IOError as msg:
            print("Cannot write config file %s:\n%s" % (filepath, msg),
                  file=sys.stderr)
            return 0
    return -1


def filter2string(f):
    s = '~' if f['inverse'] else ''
    s += f['name']
    if f['name'] in gconfig.FILTERS_WITH_PARAM:
        s += '=' + f[f['name']]
    return s


def parse_sort_str(sort_str, orders):
    sort_orders = []
    for i in sort_str.split(','):
        x = i.split(':')
        if len(x) > 1 and x[1] in orders:
            sort_orders.append({'name': x[1], 'reverse': True})
        elif x[0] in orders:
            sort_orders.append({'name': x[0], 'reverse': False})
    if sort_orders == []:
        sort_orders = [{'name': 'name', 'reverse': False}]
    return sort_orders


def parse_filter_str(s):
    s = s.split(" #& ")
    ret = []
    for t in s:
        ret.append(parse_single_filter_str(t))
    return ret


def parse_single_filter_str(s):
    if s == '':
        return {'name': '', 'inverse': False}
    s = s.split('#=')
    if len(s) == 0 or len(s) % 2 == 1:
        return [{'name': '', 'inverse': False}]
    ret = []
    for i in range(0, len(s), 2):
        f = {}
        if s[i].startswith(':'):
            f['inverse'] = True
            s[i] = s[i][1:]
        else:
            f['inverse'] = False
        f['name'] = s[i]
        if s[i] in GConfig.FILTERS_WITH_PARAM:
            f[s[i]] = s[i + 1]
        ret.append(f)
    return ret


def parse_config_profiles(config, orders):
    if 'Profiles' not in config:
        return {}
    ret = {}
    for i in config['Profiles']:
        if i.startswith('profile'):
            name = i[7:]
            if name:
                s = config['Profiles'][i].rsplit('#=', 1)
                if s == ['']:
                    ret[name] = {'sort': [{'name': 'name', 'reverse': False}], 'filter': [[{'name': '', 'inverse': False}]]}
                elif len(s) == 2:
                    ret[name] = {}
                    ret[name]['sort'] = parse_sort_str(s[1], orders)
                    ret[name]['filter'] = parse_filter_str(s[0])
    return ret


def get_key(key):
    if key == 'ENTER':
        return (K.LF, K.CR, curses.KEY_ENTER)
    if len(key) == 2 and key[0] == '^':
        # Convert usual ctrl notation (^a) to ours (a_)
        key = key[1] + '_'
    if len(key) == 1:
        return (ord(key),)
    key = key.upper()
    k = getattr(K, key, ())
    try:
        return (k or getattr(curses, 'KEY_'+key),)
    except AttributeError:
        return ()

def set_key(key, key_actions, interface, action, delete=None):
    for k in get_key(key):
        key_actions[k] = getattr(interface, 'action_'+action, lambda: None)
        if delete and k in delete:
            del delete[k]

def set_keys(actions, key_actions, accepted, interface):
    for a in actions:
        if actions[a][0] & 15 in accepted:
            for k in actions[a][1]:
                set_key(k, key_actions, interface, a)

def parse_config_key(interface, config, gconfig, common_keys, details_keys, list_keys, action_keys):
    sections = {'ListKeys': list_keys, 'CommonKeys': common_keys, 'DetailsKeys': details_keys}
    for section in sections:
        if section in config:
            for key in config[section]:
                if config[section][key] in gconfig.actions:
                    set_key(key, sections[section], interface, config[section][key], common_keys if section != 'CommonKeys' else None)
                    for k in action_keys.values():
                        k.discard(key)
    if 'cancel' in config['Misc']:
        gconfig.esc_keys = tuple()
        for i in config['Misc']['cancel'].split(','):
            k = get_key(i)
            if k:
                gconfig.esc_keys += k
    else:
        gconfig.esc_keys = (K.ESC, K.q, curses.KEY_BREAK)
    gconfig.esc_keys_no_ascii = tuple(x for x in gconfig.esc_keys if x not in range(32, 127))
    gconfig.esc_keys_w = gconfig.esc_keys + (K.W_,)
    gconfig.esc_keys_w_enter = gconfig.esc_keys_w + (K.LF, K.CR, curses.KEY_ENTER)
    gconfig.esc_keys_w_no_ascii = tuple(x for x in gconfig.esc_keys_w if x not in range(32, 127))


def list_keys():
    print('ASCII:')
    names = {}
    for k in dir(Keys):
        if 'A' <= k[0] <= 'Z':
            names[getattr(Keys, k)] = k
    for i in range(32, 127):
        if i < K.n0 or (K.n9 < i < K.A) or (K.Z < i < K.a) or (i > K.z):
            print(chr(i), '  ', names[i])
    print('\nCurses:\n' + ', '.join(x[4:] for x in dir(curses) if x[:4] == 'KEY_'))


def list_actions(actions):
    modes = {0: 'both', 1: 'list', 2: 'details', 3: 'details', 4: 'details', 16: 'movement'}
    for a, d in actions.items():
        print(a.ljust(36), modes[d[0] & 255].ljust(8), '/'.join(d[1]).rjust(13), d[2])


def xdg_config_home(*args):
    p = os.environ.get('XDG_CONFIG_HOME')

    if p is None or not os.path.isabs(p):
        p = os.path.expanduser('~/.config')

    return os.path.join(p, *args)


if __name__ == '__main__':
    # command line parameters
    gconfig = GConfig()

    # forward arguments after '--' to transmission-remote
    if gconfig.transmissionremote_args:
        cmd = ['transmission-remote', '%s:%s' % (gconfig.host, gconfig.port)]

        # transmission-remote requires --auth or --authenv before any other
        # parameters which require authentication. Otherwise, auth fails.
        if gconfig.username and gconfig.password:
            os.environ["TR_AUTH"] = "{0}:{1}".format(gconfig.username, gconfig.password)
            cmd.extend(['--authenv'])

        # one argument and it doesn't start with '-' --> treat it like it's a torrent link/url
        if len(gconfig.transmissionremote_args) == 1 and not gconfig.transmissionremote_args[0].startswith('-'):
            cmd.extend(['-a', gconfig.transmissionremote_args[0]])
        else:
            cmd.extend(gconfig.transmissionremote_args)

        pdebug("EXECUTING:\n%s\nRESPONSE:" % ' '.join(cmd))
        try:
            retcode = call(cmd)
        except OSError as msg:
            exit_prog("Could not execute the above command: %s\n" % msg.strerror, 128)
        exit_prog('', retcode)

    if gconfig.rdns:
        # Only import threading if needed. It was optional until python 3.7
        try:
            import threading
        except ImportError:
            gconfig.rdns = False

    norm = Normalizer()

    try:
        Interface(Transmission(gconfig.url, gconfig.username, gconfig.password))
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
    finally:
        exit_prog()