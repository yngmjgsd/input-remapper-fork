#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# input-remapper - GUI for device specific keyboard mappings
# Copyright (C) 2025 sezanzeb <b8x45ygc9@mozmail.com>
#
# This file is part of input-remapper.
#
# input-remapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# input-remapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with input-remapper.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import atexit
import multiprocessing
import os
import time
import unittest
from contextlib import contextmanager
from typing import Tuple, List, Optional, Iterable
from unittest.mock import patch, MagicMock, call

import evdev
import gi
from evdev.ecodes import (
    EV_KEY,
    EV_ABS,
    KEY_LEFTSHIFT,
    KEY_A,
    KEY_Q,
    EV_REL,
)

from inputremapper.gui.autocompletion import (
    get_incomplete_parameter,
    get_incomplete_function_name,
)
from inputremapper.injection.global_uinputs import GlobalUInputs, FrontendUInput, UInput
from inputremapper.injection.mapping_handlers.mapping_parser import MappingParser
from inputremapper.input_event import InputEvent
from tests.integration.test_components import FlowBoxTestUtils
from tests.lib.cleanup import cleanup
from tests.lib.constants import EVENT_READ_TIMEOUT
from tests.lib.fixtures import fixtures
from tests.lib.fixtures import prepare_presets
from tests.lib.logger import logger
from tests.lib.pipes import push_event, push_events, uinput_write_history_pipe
from tests.lib.spy import spy

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, GLib, Gdk, GtkSource

from inputremapper.configs.keyboard_layout import keyboard_layout
from inputremapper.configs.mapping import Mapping
from inputremapper.configs.paths import PathUtils
from inputremapper.configs.global_config import GlobalConfig
from inputremapper.groups import _Groups
from inputremapper.gui.data_manager import DataManager
from inputremapper.gui.messages.message_broker import (
    MessageBroker,
    MessageType,
)
from inputremapper.gui.messages.message_data import StatusData, CombinationRecorded
from inputremapper.gui.components.editor import (
    MappingSelectionLabel,
    SET_KEY_FIRST,
    CodeEditor,
)
from inputremapper.gui.components.device_groups import DeviceGroupEntry
from inputremapper.gui.controller import Controller
from inputremapper.gui.reader_service import ReaderService
from inputremapper.gui.utils import gtk_iteration, Colors, debounce, debounce_manager
from inputremapper.gui.user_interface import UserInterface
from inputremapper.injection.injector import InjectorState
from inputremapper.configs.input_config import InputCombination, InputConfig
from inputremapper.daemon import Daemon, DaemonProxy
from inputremapper.bin.input_remapper_gtk import InputRemapperGtkBin

from tests.lib.test_setup import test_setup


# iterate a few times when Gtk.main() is called, but don't block
# there and just continue to the tests while the UI becomes
# unresponsive
Gtk.main = gtk_iteration

# doesn't do much except avoid some Gtk assertion error, whatever:
Gtk.main_quit = lambda: None


def launch() -> Tuple[
    UserInterface,
    Controller,
    DataManager,
    MessageBroker,
    DaemonProxy,
    GlobalConfig,
]:
    """Start input-remapper-gtk."""
    return_ = InputRemapperGtkBin.main()
    gtk_iteration()
    # otherwise a new handler is added with each call to launch, which
    # spams tons of garbage when all tests finish
    atexit.unregister(InputRemapperGtkBin.stop)
    return return_


def start_reader_service():
    def process():
        global_uinputs = GlobalUInputs(FrontendUInput)
        reader_service = ReaderService(_Groups(), global_uinputs)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(reader_service.run())

    multiprocessing.Process(target=process).start()


def os_system_patch(cmd, original_os_system=os.system):
    # instead of running pkexec, fork instead. This will make
    # the reader-service aware of all the test patches
    if "pkexec input-remapper-control --command start-reader-service" in cmd:
        logger.info("pkexec-patch starting ReaderService process")
        start_reader_service()
        return 0

    return original_os_system(cmd)


@contextmanager
def patch_services():
    """Don't connect to the dbus and don't use pkexec to start the reader-service"""

    def bootstrap_daemon():
        # The daemon gets fresh instances of everything, because as far as I remember
        # it runs in a separate process.
        global_config = GlobalConfig()
        global_uinputs = GlobalUInputs(UInput)
        mapping_parser = MappingParser(global_uinputs)

        return Daemon(
            global_config,
            global_uinputs,
            mapping_parser,
        )

    with patch.object(
        os,
        "system",
        os_system_patch,
    ), patch.object(Daemon, "connect", bootstrap_daemon):
        yield


def clean_up_integration(test):
    logger.info("clean_up_integration")
    test.controller.stop_injecting()
    gtk_iteration()
    test.user_interface.on_gtk_close()
    test.user_interface.window.destroy()
    gtk_iteration()
    cleanup()

    # do this now, not when all tests are finished
    test.daemon.stop_all()
    if isinstance(test.daemon, Daemon):
        atexit.unregister(test.daemon.stop_all)


class GtkKeyEvent:
    def __init__(self, keyval):
        self.keyval = keyval

    def get_keyval(self):
        return True, self.keyval


@test_setup
class TestGroupsFromReaderService(unittest.TestCase):
    def patch_os_system(self):
        def os_system(cmd, original_os_system=os.system):
            # instead of running pkexec, fork instead. This will make
            # the reader-service aware of all the test patches
            if "pkexec input-remapper-control --command start-reader-service" in cmd:
                # don't start the reader-service just log that it was.
                self.reader_service_started()
                return 0

            return original_os_system(cmd)

        self.os_system_patch = patch.object(
            os,
            "system",
            os_system,
        )

        # this is already part of the test. we need a bit of patching and hacking
        # because we want to discover the groups as early a possible, to reduce startup
        # time for the application
        self.os_system_patch.start()

    def bootstrap_daemon(self):
        # The daemon gets fresh instances of everything, because as far as I remember
        # it runs in a separate process.
        global_config = GlobalConfig()
        global_uinputs = GlobalUInputs(UInput)
        mapping_parser = MappingParser(global_uinputs)

        return Daemon(
            global_config,
            global_uinputs,
            mapping_parser,
        )

    def patch_daemon(self):
        # don't try to connect, return an object instance of it instead
        self.daemon_connect_patch = patch.object(
            Daemon,
            "connect",
            lambda: self.bootstrap_daemon(),
        )
        self.daemon_connect_patch.start()

    def setUp(self):
        self.reader_service_started = MagicMock()
        self.patch_os_system()
        self.patch_daemon()

        (
            self.user_interface,
            self.controller,
            self.data_manager,
            self.message_broker,
            self.daemon,
            self.global_config,
        ) = launch()

    def tearDown(self):
        clean_up_integration(self)
        self.os_system_patch.stop()
        self.daemon_connect_patch.stop()

    def test_knows_devices(self):
        # verify that it is working as expected. The gui doesn't have knowledge
        # of groups until the root-reader-service provides them
        self.data_manager._reader_client.groups.set_groups([])
        gtk_iteration()
        self.reader_service_started.assert_called()
        self.assertEqual(len(self.data_manager.get_group_keys()), 0)

        # start the reader-service delayed
        start_reader_service()
        # perform some iterations so that the reader ends up reading from the pipes
        # which will make it receive devices.
        for _ in range(10):
            time.sleep(0.02)
            gtk_iteration()

        self.assertIn("Foo Device 2", self.data_manager.get_group_keys())
        self.assertIn("Foo Device 2", self.data_manager.get_group_keys())
        self.assertIn("Bar Device", self.data_manager.get_group_keys())
        self.assertIn("gamepad", self.data_manager.get_group_keys())
        self.assertEqual(self.data_manager.active_group.name, "Foo Device")


@contextmanager
def patch_confirm_delete(
    user_interface: UserInterface,
    response=Gtk.ResponseType.ACCEPT,
):
    original_create_dialog = user_interface._create_dialog

    def _create_dialog_patch(*args, **kwargs):
        """A patch for the deletion confirmation that briefly shows the dialog."""
        confirm_cancel_dialog = original_create_dialog(*args, **kwargs)

        # the emitted signal causes the dialog to close
        GLib.timeout_add(
            100,
            lambda: confirm_cancel_dialog.emit("response", response),
        )

        # don't recursively call the patch
        Gtk.MessageDialog.run(confirm_cancel_dialog)

        confirm_cancel_dialog.run = lambda: response

        return confirm_cancel_dialog

    with patch.object(
        user_interface,
        "_create_dialog",
        _create_dialog_patch,
    ):
        # Tests are run during `yield`
        yield


class GuiTestBase(unittest.TestCase):
    def setUp(self):
        prepare_presets()
        with patch_services():
            (
                self.user_interface,
                self.controller,
                self.data_manager,
                self.message_broker,
                self.daemon,
                self.global_config,
            ) = launch()

        get = self.user_interface.get
        self.device_selection: Gtk.FlowBox = get("device_selection")
        self.preset_selection: Gtk.ComboBoxText = get("preset_selection")
        self.selection_label_listbox: Gtk.ListBox = get("selection_label_listbox")
        self.target_selection: Gtk.ComboBox = get("target-selector")
        self.recording_toggle: Gtk.ToggleButton = get("key_recording_toggle")
        self.recording_status: Gtk.ToggleButton = get("recording_status")
        self.status_bar: Gtk.Statusbar = get("status_bar")
        self.autoload_toggle: Gtk.Switch = get("preset_autoload_switch")
        self.code_editor: GtkSource.View = get("code_editor")
        self.output_box: GtkSource.View = get("output")

        self.delete_preset_btn: Gtk.Button = get("delete_preset")
        self.copy_preset_btn: Gtk.Button = get("copy_preset")
        self.create_preset_btn: Gtk.Button = get("create_preset")
        self.start_injector_btn: Gtk.Button = get("apply_preset")
        self.stop_injector_btn: Gtk.Button = get("stop_injection_preset_page")
        self.rename_btn: Gtk.Button = get("rename-button")
        self.rename_input: Gtk.Entry = get("preset_name_input")
        self.create_mapping_btn: Gtk.Button = get("create_mapping_button")
        self.delete_mapping_btn: Gtk.Button = get("delete-mapping")

        self._test_initial_state()

        self.grab_fails = False

        def grab(_):
            if self.grab_fails:
                raise OSError()

        evdev.InputDevice.grab = grab

        self.global_config._save_config()

        self.throttle(20)

        self.assertIsNotNone(self.data_manager.active_group)
        self.assertIsNotNone(self.data_manager.active_preset)

    def tearDown(self):
        clean_up_integration(self)

        # this is important, otherwise it keeps breaking things in the background
        self.assertIsNone(self.data_manager._reader_client._read_timeout)

        self.throttle(20)

    def get_code_input(self):
        buffer = self.code_editor.get_buffer()
        return buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            True,
        )

    def _test_initial_state(self):
        # make sure each test deals with the same initial state
        self.assertEqual(self.controller.data_manager, self.data_manager)
        self.assertEqual(self.data_manager.active_group.key, "Foo Device")
        # if the modification-date from `prepare_presets` is not destroyed, preset3
        # should be selected as the newest one
        self.assertEqual(self.data_manager.active_preset.name, "preset3")
        self.assertEqual(self.data_manager.active_mapping.target_uinput, "keyboard")
        self.assertEqual(self.target_selection.get_active_id(), "keyboard")
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination([InputConfig(type=1, code=5)]),
        )
        self.assertEqual(
            self.data_manager.active_input_config, InputConfig(type=1, code=5)
        )
        self.assertGreater(
            len(self.user_interface.autocompletion._target_key_capabilities), 0
        )

    def _callTestMethod(self, method):
        """Retry all tests if they fail.

        GUI tests suddenly started to lag a lot and fail randomly, and even
        though that improved drastically, sometimes they still do.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                method()
                break
            except Exception as e:
                if attempts == 2:
                    raise e

            # try again
            print("Test failed, trying again...")
            self.tearDown()
            self.setUp()

    def throttle(self, time_=10):
        """Give GTK some time in ms to process everything."""
        # tests suddenly started to freeze my computer up completely and tests started
        # to fail. By using this (and by optimizing some redundant calls in the gui) it
        # worked again. EDIT: Might have been caused by my broken/bloated ssd. I'll
        # keep it in some places, since it did make the tests more reliable after all.
        for _ in range(time_ // 2):
            gtk_iteration()
            time.sleep(0.002)

    def set_focus(self, widget):
        logger.info("Focusing %s", widget)

        self.user_interface.window.set_focus(widget)

        self.throttle(20)

    def focus_source_view(self):
        # despite the focus and gtk_iterations, gtk never runs the event handlers for
        # the focus-in-event (_update_placeholder), which would clear the placeholder
        # text. Remove it manually, it can't be helped. Fun fact: when the
        # window gets destroyed, gtk runs the handler 10 times for good measure.
        # Lost one hour of my life on GTK again. It's gone! Forever! Use qt next time.
        source_view = self.code_editor
        self.set_focus(source_view)
        self.code_editor.get_buffer().set_text("")
        return source_view

    def get_selection_labels(self) -> List[MappingSelectionLabel]:
        return self.selection_label_listbox.get_children()

    def get_status_text(self):
        status_bar = self.user_interface.get("status_bar")
        return status_bar.get_message_area().get_children()[0].get_label()

    def get_unfiltered_symbol_input_text(self):
        buffer = self.code_editor.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def select_mapping(self, i: int):
        """Select one of the mappings of a preset.

        Parameters
        ----------
        i
            if -1, will select the last row,
            0 will select the uppermost row.
            1 will select the second row, and so on
        """
        selection_label = self.get_selection_labels()[i]
        self.selection_label_listbox.select_row(selection_label)
        logger.info(
            'Selecting mapping %s "%s"',
            selection_label.combination,
            selection_label.name,
        )
        gtk_iteration()
        return selection_label

    def add_mapping(self, mapping: Optional[Mapping] = None):
        self.controller.create_mapping()
        self.controller.load_mapping(InputCombination.empty_combination())
        gtk_iteration()
        if mapping:
            self.controller.update_mapping(**mapping.dict(exclude_defaults=True))
            gtk_iteration()

    def sleep(self, num_events):
        for _ in range(num_events * 2):
            time.sleep(EVENT_READ_TIMEOUT)
            gtk_iteration()

        time.sleep(1 / 30)  # one window iteration

        gtk_iteration()


@test_setup
class TestColors(GuiTestBase):
    # requires a running ui, otherwise fails with segmentation faults
    def test_get_color_falls_back(self):
        fallback = Gdk.RGBA(0, 0.5, 1, 0.8)

        color = Colors.get_color(["doesnt_exist_1234"], fallback)

        self.assertIsInstance(color, Gdk.RGBA)
        self.assertAlmostEqual(color.red, fallback.red, delta=0.01)
        self.assertAlmostEqual(color.green, fallback.green, delta=0.01)
        self.assertAlmostEqual(color.blue, fallback.blue, delta=0.01)
        self.assertAlmostEqual(color.alpha, fallback.alpha, delta=0.01)

    def test_get_color_works(self):
        fallback = Gdk.RGBA(1, 0, 1, 0.1)

        color = Colors.get_color(
            ["accent_bg_color", "theme_selected_bg_color"], fallback
        )

        self.assertIsInstance(color, Gdk.RGBA)
        self.assertNotAlmostEqual(color.red, fallback.red, delta=0.01)
        self.assertNotAlmostEqual(color.green, fallback.blue, delta=0.01)
        self.assertNotAlmostEqual(color.blue, fallback.green, delta=0.01)
        self.assertNotAlmostEqual(color.alpha, fallback.alpha, delta=0.01)

    def _test_color_wont_fallback(self, get_color, fallback):
        color = get_color()
        self.assertIsInstance(color, Gdk.RGBA)
        if (
            (abs(color.green - fallback.green) < 0.01)
            and (abs(color.red - fallback.red) < 0.01)
            and (abs(color.blue - fallback.blue) < 0.01)
            and (abs(color.alpha - fallback.alpha) < 0.01)
        ):
            raise AssertionError(
                f"Color {color.to_string()} is similar to {fallback.toString()}"
            )

    def test_get_colors(self):
        self._test_color_wont_fallback(Colors.get_accent_color, Colors.fallback_accent)
        self._test_color_wont_fallback(Colors.get_border_color, Colors.fallback_border)
        self._test_color_wont_fallback(
            Colors.get_background_color, Colors.fallback_background
        )
        self._test_color_wont_fallback(Colors.get_base_color, Colors.fallback_base)
        self._test_color_wont_fallback(Colors.get_font_color, Colors.fallback_font)


@test_setup
class TestGui(GuiTestBase):
    """For tests that use the window.

    It is intentional that there is no access to the Components.
    Try to modify the configuration only by calling functions of the window.
    For example by simulating clicks on buttons. Get the widget to interact with
    by going through the windows children. (See click_on_group for inspiration)
    """

    def click_on_group(self, group_key: str):
        for child in self.device_selection.get_children():
            device_group_entry = child.get_children()[0]

            if device_group_entry.group_key == group_key:
                device_group_entry.set_active(True)

    def test_can_start(self):
        self.assertIsNotNone(self.user_interface)
        self.assertTrue(self.user_interface.window.get_visible())

    def assert_gui_clean(self):
        selection_labels = self.selection_label_listbox.get_children()
        self.assertEqual(len(selection_labels), 0)
        self.assertEqual(len(self.data_manager.active_preset), 0)
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name, "new preset"
        )
        self.assertEqual(self.recording_toggle.get_label(), "Record")
        self.assertEqual(self.get_unfiltered_symbol_input_text(), SET_KEY_FIRST)

    def test_initial_state(self):
        self.assertEqual(self.data_manager.active_group.key, "Foo Device")
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.device_selection).name, "Foo Device"
        )
        self.assertEqual(self.data_manager.active_preset.name, "preset3")
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name, "preset3"
        )
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination([InputConfig(type=1, code=5)]),
        )
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination(
                [
                    InputConfig(type=1, code=5),
                ]
            ),
        )
        self.assertEqual(self.selection_label_listbox.get_selected_row().name, "4")
        self.assertIsNone(self.data_manager.active_mapping.name)
        self.assertTrue(self.data_manager.active_mapping.is_valid())
        self.assertTrue(self.data_manager.active_preset.is_valid())
        # todo

    def test_set_autoload_refreshes_service_config(self):
        self.assertFalse(self.data_manager.get_autoload())
        with spy(self.daemon, "set_config_dir") as set_config_dir:
            self.autoload_toggle.set_active(True)
            gtk_iteration()
            set_config_dir.assert_called_once()
        self.assertTrue(self.data_manager.get_autoload())

    def test_autoload_sets_correctly(self):
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())

        self.autoload_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

        self.autoload_toggle.set_active(False)
        gtk_iteration()
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())

    def test_autoload_is_set_when_changing_preset(self):
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())

        self.click_on_group("Foo Device 2")
        FlowBoxTestUtils.set_active(self.preset_selection, "preset2")

        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

    def test_only_one_autoload_per_group(self):
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())

        self.click_on_group("Foo Device 2")
        FlowBoxTestUtils.set_active(self.preset_selection, "preset2")
        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

        FlowBoxTestUtils.set_active(self.preset_selection, "preset3")
        gtk_iteration()
        self.autoload_toggle.set_active(True)
        gtk_iteration()
        FlowBoxTestUtils.set_active(self.preset_selection, "preset2")
        gtk_iteration()
        self.assertFalse(self.data_manager.get_autoload())
        self.assertFalse(self.autoload_toggle.get_active())

    def test_each_device_can_have_autoload(self):
        self.autoload_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

        self.click_on_group("Foo Device 2")
        gtk_iteration()
        self.autoload_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

        self.click_on_group("Foo Device")
        gtk_iteration()
        self.assertTrue(self.data_manager.get_autoload())
        self.assertTrue(self.autoload_toggle.get_active())

    def test_select_device_without_preset(self):
        # creates a new empty preset when no preset exists for the device
        self.click_on_group("Bar Device")
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name, "new preset"
        )
        self.assertEqual(len(self.data_manager.active_preset), 0)

        # it creates the file for that right away. It may have been possible
        # to write it such that it doesn't (its empty anyway), but it does,
        # so use that to test it in more detail.
        path = PathUtils.get_preset_path("Bar Device", "new preset")
        self.assertTrue(os.path.exists(path))
        with open(path, "r") as file:
            self.assertEqual(file.read(), "")

    def test_recording_toggle_labels(self):
        self.assertFalse(self.recording_status.get_visible())

        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.recording_status.get_visible())

        self.recording_toggle.set_active(False)
        gtk_iteration()
        self.assertFalse(self.recording_status.get_visible())

    def test_recording_label_updates_on_recording_finished(self):
        self.assertFalse(self.recording_status.get_visible())

        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.recording_status.get_visible())

        self.message_broker.signal(MessageType.recording_finished)
        gtk_iteration()
        self.assertFalse(self.recording_status.get_visible())
        self.assertFalse(self.recording_toggle.get_active())

    def test_events_from_reader_service_arrive(self):
        # load a device with more capabilities
        self.controller.load_group("Foo Device 2")
        gtk_iteration()
        mock1 = MagicMock()
        mock2 = MagicMock()
        mock3 = MagicMock()
        self.message_broker.subscribe(MessageType.combination_recorded, mock1)
        self.message_broker.subscribe(MessageType.recording_finished, mock2)
        self.message_broker.subscribe(MessageType.recording_started, mock3)
        self.recording_toggle.set_active(True)
        mock3.assert_called_once()
        gtk_iteration()

        push_events(
            fixtures.foo_device_2_keyboard,
            [
                InputEvent(0, 0, 1, 30, 1),
                InputEvent(0, 0, 1, 31, 1),
            ],
        )
        self.throttle(60)
        origin = fixtures.foo_device_2_keyboard.get_device_hash()
        mock1.assert_has_calls(
            (
                call(
                    CombinationRecorded(
                        InputCombination(
                            [InputConfig(type=1, code=30, origin_hash=origin)]
                        )
                    )
                ),
                call(
                    CombinationRecorded(
                        InputCombination(
                            [
                                InputConfig(type=1, code=30, origin_hash=origin),
                                InputConfig(type=1, code=31, origin_hash=origin),
                            ]
                        )
                    )
                ),
            ),
            any_order=False,
        )
        self.assertEqual(mock1.call_count, 2)
        mock2.assert_not_called()

        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 31, 0)])
        self.throttle(60)
        self.assertEqual(mock1.call_count, 2)
        mock2.assert_not_called()

        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 30, 0)])
        self.throttle(60)
        self.assertEqual(mock1.call_count, 2)
        mock2.assert_called_once()

        self.assertFalse(self.recording_toggle.get_active())
        mock3.assert_called_once()

    def test_cannot_create_duplicate_input_combination(self):
        # load a device with more capabilities
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        # update the combination of the active mapping
        self.controller.start_key_recording()
        push_events(
            fixtures.foo_device_2_keyboard,
            [InputEvent(0, 0, 1, 30, 1), InputEvent(0, 0, 1, 30, 0)],
        )
        self.throttle(60)

        # if this fails with <InputCombination (1, 5, 1)>: this is the initial
        # mapping or something, so it was never overwritten.
        origin = fixtures.foo_device_2_keyboard.get_device_hash()
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination([InputConfig(type=1, code=30, origin_hash=origin)]),
        )

        # create a new mapping
        self.controller.create_mapping()
        gtk_iteration()
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination.empty_combination(),
        )

        # try to record the same combination
        self.controller.start_key_recording()
        push_events(
            fixtures.foo_device_2_keyboard,
            [InputEvent(0, 0, 1, 30, 1), InputEvent(0, 0, 1, 30, 0)],
        )
        self.throttle(60)
        # should still be the empty mapping
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination.empty_combination(),
        )

        # try to record a different combination
        self.controller.start_key_recording()
        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 30, 1)])
        self.throttle(60)
        # nothing changed yet, as we got the duplicate combination
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination.empty_combination(),
        )
        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 31, 1)])
        self.throttle(60)
        # now the combination is different
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination(
                [
                    InputConfig(type=1, code=30, origin_hash=origin),
                    InputConfig(type=1, code=31, origin_hash=origin),
                ]
            ),
        )

        # let's make the combination even longer
        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 32, 1)])
        self.throttle(60)
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination(
                [
                    InputConfig(type=1, code=30, origin_hash=origin),
                    InputConfig(type=1, code=31, origin_hash=origin),
                    InputConfig(type=1, code=32, origin_hash=origin),
                ]
            ),
        )

        # make sure we stop recording by releasing all keys
        push_events(
            fixtures.foo_device_2_keyboard,
            [
                InputEvent(0, 0, 1, 31, 0),
                InputEvent(0, 0, 1, 30, 0),
                InputEvent(0, 0, 1, 32, 0),
            ],
        )
        self.throttle(60)

        # sending a combination update now should not do anything
        self.message_broker.publish(
            CombinationRecorded(InputCombination([InputConfig(type=1, code=35)]))
        )
        gtk_iteration()
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination(
                [
                    InputConfig(type=1, code=30, origin_hash=origin),
                    InputConfig(type=1, code=31, origin_hash=origin),
                    InputConfig(type=1, code=32, origin_hash=origin),
                ]
            ),
        )

    def test_create_simple_mapping(self):
        self.click_on_group("Foo Device 2")
        # 1. create a mapping
        self.create_mapping_btn.clicked()
        gtk_iteration()

        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination.empty_combination(),
        )
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination.empty_combination(),
        )
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().name, "Empty Mapping"
        )
        self.assertIsNone(self.data_manager.active_mapping.name)

        # there are now 2 mappings
        self.assertEqual(len(self.selection_label_listbox.get_children()), 2)
        self.assertEqual(len(self.data_manager.active_preset), 2)

        # 2. record a combination for that mapping
        self.recording_toggle.set_active(True)
        gtk_iteration()
        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 30, 1)])
        self.throttle(60)
        push_events(fixtures.foo_device_2_keyboard, [InputEvent(0, 0, 1, 30, 0)])
        self.throttle(60)

        # check the input_combination
        origin = fixtures.foo_device_2_keyboard.get_device_hash()
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination([InputConfig(type=1, code=30, origin_hash=origin)]),
        )
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination([InputConfig(type=1, code=30, origin_hash=origin)]),
        )
        self.assertEqual(self.selection_label_listbox.get_selected_row().name, "a")
        self.assertIsNone(self.data_manager.active_mapping.name)

        # 3. set the output symbol
        self.code_editor.get_buffer().set_text("Shift_L")
        gtk_iteration()

        # the mapping and preset should be valid by now
        self.assertTrue(self.data_manager.active_mapping.is_valid())
        self.assertTrue(self.data_manager.active_preset.is_valid())

        self.assertEqual(
            self.data_manager.active_mapping,
            Mapping(
                input_combination=InputCombination(
                    [InputConfig(type=1, code=30, origin_hash=origin)]
                ),
                output_symbol="Shift_L",
                target_uinput="keyboard",
            ),
        )
        self.assertEqual(self.target_selection.get_active_id(), "keyboard")
        buffer = self.code_editor.get_buffer()
        self.assertEqual(
            buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
            "Shift_L",
        )
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination([InputConfig(type=1, code=30, origin_hash=origin)]),
        )

        # 4. update target
        self.target_selection.set_active_id("keyboard + mouse")
        gtk_iteration()
        self.assertEqual(
            self.data_manager.active_mapping,
            Mapping(
                input_combination=InputCombination(
                    [InputConfig(type=1, code=30, origin_hash=origin)]
                ),
                output_symbol="Shift_L",
                target_uinput="keyboard + mouse",
            ),
        )

    def test_show_status(self):
        self.message_broker.publish(StatusData(0, "a"))
        text = self.get_status_text()
        self.assertEqual("a", text)

    def test_hat_switch(self):
        # load a device with more capabilities
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        # it should be possible to add all of them
        ev_1 = InputEvent.abs(evdev.ecodes.ABS_HAT0X, -1)
        ev_2 = InputEvent.abs(evdev.ecodes.ABS_HAT0X, 1)
        ev_3 = InputEvent.abs(evdev.ecodes.ABS_HAT0Y, -1)
        ev_4 = InputEvent.abs(evdev.ecodes.ABS_HAT0Y, 1)

        def add_mapping(event, symbol) -> InputCombination:
            """adds mapping and returns the expected input combination"""
            self.controller.create_mapping()
            gtk_iteration()
            self.controller.start_key_recording()
            push_events(fixtures.foo_device_2_gamepad, [event, event.modify(value=0)])
            self.throttle(60)
            gtk_iteration()
            self.code_editor.get_buffer().set_text(symbol)
            gtk_iteration()
            return InputCombination(
                [
                    InputConfig.from_input_event(event).modify(
                        origin_hash=fixtures.foo_device_2_gamepad.get_device_hash()
                    )
                ]
            )

        config_1 = add_mapping(ev_1, "a")
        config_2 = add_mapping(ev_2, "b")
        config_3 = add_mapping(ev_3, "c")
        config_4 = add_mapping(ev_4, "d")

        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                InputCombination(config_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                InputCombination(config_2)
            ).output_symbol,
            "b",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                InputCombination(config_3)
            ).output_symbol,
            "c",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                InputCombination(config_4)
            ).output_symbol,
            "d",
        )

    def test_combination(self):
        # if this test freezes, try waiting a few minutes and then look for
        # stack traces in the console

        # load a device with more capabilities
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        # it should be possible to write a combination
        ev_1 = InputEvent.key(
            evdev.ecodes.KEY_A,
            1,
            origin_hash=fixtures.foo_device_2_keyboard.get_device_hash(),
        )
        ev_2 = InputEvent.abs(
            evdev.ecodes.ABS_HAT0X,
            1,
            origin_hash=fixtures.foo_device_2_gamepad.get_device_hash(),
        )
        ev_3 = InputEvent.key(
            evdev.ecodes.KEY_C,
            1,
            origin_hash=fixtures.foo_device_2_keyboard.get_device_hash(),
        )
        ev_4 = InputEvent.abs(
            evdev.ecodes.ABS_HAT0X,
            -1,
            origin_hash=fixtures.foo_device_2_gamepad.get_device_hash(),
        )
        combination_1 = (ev_1, ev_2, ev_3)
        combination_2 = (ev_2, ev_1, ev_3)

        # same as 1, but different D-Pad direction
        combination_3 = (ev_1, ev_4, ev_3)
        combination_4 = (ev_4, ev_1, ev_3)

        # same as 1, but the last combination is different
        combination_5 = (ev_1, ev_3, ev_2)
        combination_6 = (ev_3, ev_1, ev_2)

        def get_combination(combi: Iterable[InputEvent]) -> InputCombination:
            """Create an InputCombination from a list of events.

            Ensures the origin_hash is set correctly.
            """
            configs = []
            for event in combi:
                config = InputConfig.from_input_event(event)
                configs.append(config)
            return InputCombination(configs)

        def add_mapping(combi: Iterable[InputEvent], symbol):
            logger.info("add_mapping %s", combi)
            self.controller.create_mapping()
            gtk_iteration()
            self.controller.start_key_recording()
            for event in combi:
                if event.type == EV_KEY:
                    push_event(fixtures.foo_device_2_keyboard, event)
                if event.type == EV_ABS:
                    push_event(fixtures.foo_device_2_gamepad, event)
                if event.type == EV_REL:
                    push_event(fixtures.foo_device_2_mouse, event)

                # avoid race condition if we switch fixture in push_event. The order
                # of events needs to be correct.
                self.throttle(20)

            for event in combi:
                if event.type == EV_KEY:
                    push_event(fixtures.foo_device_2_keyboard, event.modify(value=0))
                if event.type == EV_ABS:
                    push_event(fixtures.foo_device_2_gamepad, event.modify(value=0))
                if event.type == EV_REL:
                    pass

            self.throttle(60)
            gtk_iteration()
            self.code_editor.get_buffer().set_text(symbol)
            gtk_iteration()

        add_mapping(combination_1, "a")

        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_2)
            ).output_symbol,
            "a",
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_3))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_4))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_5))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_6))
        )

        # it won't write the same combination again, even if the
        # first two events are in a different order
        add_mapping(combination_2, "b")
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_2)
            ).output_symbol,
            "a",
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_3))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_4))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_5))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_6))
        )

        add_mapping(combination_3, "c")
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_2)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_3)
            ).output_symbol,
            "c",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_4)
            ).output_symbol,
            "c",
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_5))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_6))
        )

        # same as with combination_2, the existing combination_3 blocks
        # combination_4 because they have the same keys and end in the
        # same key.
        add_mapping(combination_4, "d")
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_2)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_3)
            ).output_symbol,
            "c",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_4)
            ).output_symbol,
            "c",
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_5))
        )
        self.assertIsNone(
            self.data_manager.active_preset.get_mapping(get_combination(combination_6))
        )

        add_mapping(combination_5, "e")
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_1)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_2)
            ).output_symbol,
            "a",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_3)
            ).output_symbol,
            "c",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_4)
            ).output_symbol,
            "c",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_5)
            ).output_symbol,
            "e",
        )
        self.assertEqual(
            self.data_manager.active_preset.get_mapping(
                get_combination(combination_6)
            ).output_symbol,
            "e",
        )

        error_icon = self.user_interface.get("error_status_icon")
        warning_icon = self.user_interface.get("warning_status_icon")

        self.assertFalse(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

    def test_only_one_empty_mapping_possible(self):
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination([InputConfig(type=1, code=5)]),
        )
        self.assertEqual(len(self.selection_label_listbox.get_children()), 1)
        self.assertEqual(len(self.data_manager.active_preset), 1)

        self.create_mapping_btn.clicked()
        gtk_iteration()
        self.assertEqual(
            self.selection_label_listbox.get_selected_row().combination,
            InputCombination.empty_combination(),
        )
        self.assertEqual(len(self.selection_label_listbox.get_children()), 2)
        self.assertEqual(len(self.data_manager.active_preset), 2)

        self.create_mapping_btn.clicked()
        gtk_iteration()
        self.assertEqual(len(self.selection_label_listbox.get_children()), 2)
        self.assertEqual(len(self.data_manager.active_preset), 2)

    def test_selection_labels_sort_alphabetically(self):
        self.controller.load_preset("preset1")
        # contains two mappings (1,1,1 -> b) and (1,2,1 -> a)
        gtk_iteration()
        # we expect (1,2,1 -> a) to be selected because "1" < "Escape"
        self.assertEqual(self.data_manager.active_mapping.output_symbol, "a")
        self.assertIs(
            self.selection_label_listbox.get_row_at_index(0),
            self.selection_label_listbox.get_selected_row(),
        )

        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.message_broker.publish(
            CombinationRecorded(
                InputCombination([InputConfig(type=EV_KEY, code=KEY_Q)])
            )
        )
        gtk_iteration()
        self.message_broker.signal(MessageType.recording_finished)
        gtk_iteration()
        # the combination and the order changed "Escape" < "q"
        self.assertEqual(self.data_manager.active_mapping.output_symbol, "a")
        self.assertIs(
            self.selection_label_listbox.get_row_at_index(1),
            self.selection_label_listbox.get_selected_row(),
        )

    def test_selection_labels_sort_empty_mapping_to_the_bottom(self):
        # make sure we have a mapping which would sort to the bottom only
        # considering alphanumeric sorting: "q" > "Empty Mapping"
        self.controller.load_preset("preset1")
        gtk_iteration()
        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.message_broker.publish(
            CombinationRecorded(
                InputCombination([InputConfig(type=EV_KEY, code=KEY_Q)])
            )
        )
        gtk_iteration()
        self.message_broker.signal(MessageType.recording_finished)
        gtk_iteration()

        self.controller.create_mapping()
        gtk_iteration()
        row: MappingSelectionLabel = self.selection_label_listbox.get_selected_row()
        self.assertEqual(row.combination, InputCombination.empty_combination())
        self.assertEqual(row.label.get_text(), "Empty Mapping")
        self.assertIs(self.selection_label_listbox.get_row_at_index(2), row)

    def test_select_mapping(self):
        self.controller.load_preset("preset1")
        # contains two mappings (1,1,1 -> b) and (1,2,1 -> a)
        gtk_iteration()
        # we expect (1,2,1 -> a) to be selected because "1" < "Escape"
        self.assertEqual(self.data_manager.active_mapping.output_symbol, "a")

        # select the second entry in the listbox
        row = self.selection_label_listbox.get_row_at_index(1)
        self.selection_label_listbox.select_row(row)
        gtk_iteration()
        self.assertEqual(self.data_manager.active_mapping.output_symbol, "b")

    def test_selection_label_uses_name_if_available(self):
        self.controller.load_preset("preset1")
        gtk_iteration()
        row: MappingSelectionLabel = self.selection_label_listbox.get_selected_row()
        self.assertEqual(row.label.get_text(), "1")
        self.assertIs(row, self.selection_label_listbox.get_row_at_index(0))

        self.controller.update_mapping(name="foo")
        gtk_iteration()
        self.assertEqual(row.label.get_text(), "foo")
        self.assertIs(row, self.selection_label_listbox.get_row_at_index(1))

        # Empty Mapping still sorts to the bottom
        self.controller.create_mapping()
        gtk_iteration()
        row = self.selection_label_listbox.get_selected_row()
        self.assertEqual(row.combination, InputCombination.empty_combination())
        self.assertEqual(row.label.get_text(), "Empty Mapping")
        self.assertIs(self.selection_label_listbox.get_row_at_index(2), row)

    def test_fake_empty_mapping_does_not_sort_to_bottom(self):
        """If someone chooses to name a mapping "Empty Mapping"
        it is not sorted to the bottom"""
        self.controller.load_preset("preset1")
        gtk_iteration()

        self.controller.update_mapping(name="Empty Mapping")
        self.throttle(20)  # sorting seems to take a bit

        # "Empty Mapping" < "Escape" so we still expect this to be the first row
        row = self.selection_label_listbox.get_selected_row()
        self.assertIs(row, self.selection_label_listbox.get_row_at_index(0))

        # now create a real empty mapping
        self.controller.create_mapping()
        self.throttle(20)

        # for some reason we no longer can use assertIs maybe a gtk bug?
        # self.assertIs(row, self.selection_label_listbox.get_row_at_index(0))

        # we expect the fake empty mapping in row 0 and the real one in row 2
        self.selection_label_listbox.select_row(
            self.selection_label_listbox.get_row_at_index(0)
        )
        gtk_iteration()
        self.assertEqual(self.data_manager.active_mapping.name, "Empty Mapping")
        self.assertEqual(self.data_manager.active_mapping.output_symbol, "a")

        self.selection_label_listbox.select_row(
            self.selection_label_listbox.get_row_at_index(2)
        )
        self.assertIsNone(self.data_manager.active_mapping.name)
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            InputCombination.empty_combination(),
        )

    def test_remove_mapping(self):
        self.controller.load_preset("preset1")
        gtk_iteration()
        self.assertEqual(len(self.data_manager.active_preset), 2)
        self.assertEqual(len(self.selection_label_listbox.get_children()), 2)

        with patch_confirm_delete(self.user_interface):
            self.delete_mapping_btn.clicked()
            gtk_iteration()

        self.assertEqual(len(self.data_manager.active_preset), 1)
        self.assertEqual(len(self.selection_label_listbox.get_children()), 1)

    def test_problematic_combination(self):
        # load a device with more capabilities
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        def add_mapping(combi: Iterable[Tuple[int, int, int]], symbol):
            combi = [InputEvent(0, 0, *t) for t in combi]
            self.controller.create_mapping()
            gtk_iteration()
            self.controller.start_key_recording()
            push_events(fixtures.foo_device_2_keyboard, combi)
            push_events(
                fixtures.foo_device_2_keyboard,
                [event.modify(value=0) for event in combi],
            )
            self.throttle(60)
            gtk_iteration()
            self.code_editor.get_buffer().set_text(symbol)
            gtk_iteration()

        combination = [(EV_KEY, KEY_LEFTSHIFT, 1), (EV_KEY, 82, 1)]

        add_mapping(combination, "b")
        text = self.get_status_text()
        self.assertIn("shift", text)

        error_icon = self.user_interface.get("error_status_icon")
        warning_icon = self.user_interface.get("warning_status_icon")

        self.assertFalse(error_icon.get_visible())
        self.assertTrue(warning_icon.get_visible())

    def test_rename_and_save(self):
        # only a basic test, TestController and TestDataManager go more in detail
        self.rename_input.set_text("foo")
        self.rename_btn.clicked()
        gtk_iteration()

        preset_path = f"{PathUtils.config_path()}/presets/Foo Device/foo.json"
        self.assertTrue(os.path.exists(preset_path))
        error_icon = self.user_interface.get("error_status_icon")
        self.assertFalse(error_icon.get_visible())

        def save():
            raise PermissionError

        with patch.object(self.data_manager.active_preset, "save", save):
            self.code_editor.get_buffer().set_text("f")
            gtk_iteration()
        status = self.get_status_text()
        self.assertIn("Permission denied", status)

        with patch_confirm_delete(self.user_interface):
            self.delete_preset_btn.clicked()
            gtk_iteration()
        self.assertFalse(os.path.exists(preset_path))

    def test_check_for_unknown_symbols(self):
        first_input = InputCombination([InputConfig(type=1, code=1)])
        second_input = InputCombination([InputConfig(type=1, code=2)])
        status = self.user_interface.get("status_bar")
        error_icon = self.user_interface.get("error_status_icon")
        warning_icon = self.user_interface.get("warning_status_icon")

        self.controller.load_preset("preset1")
        self.throttle(20)

        # Switch to the first mapping, and change it
        self.controller.load_mapping(first_input)
        gtk_iteration()
        self.controller.update_mapping(output_symbol="foo")
        gtk_iteration()

        # Switch to the second mapping, and change it
        self.controller.load_mapping(second_input)
        gtk_iteration()
        self.controller.update_mapping(output_symbol="qux")
        gtk_iteration()

        # The tooltip should show the error of the currently selected mapping
        tooltip = status.get_tooltip_text().lower()
        self.assertIn("qux", tooltip)
        self.assertTrue(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

        # So switching to the other mapping changes the tooltip
        self.controller.load_mapping(first_input)
        gtk_iteration()
        tooltip = status.get_tooltip_text().lower()
        self.assertIn("foo", tooltip)

        # It will still save it though
        with open(PathUtils.get_preset_path("Foo Device", "preset1")) as f:
            content = f.read()
            self.assertIn("qux", content)
            self.assertIn("foo", content)

        # Fix the current active mapping.
        # It should show the error of the other mapping now.
        self.controller.update_mapping(output_symbol="a")
        gtk_iteration()
        tooltip = status.get_tooltip_text().lower()
        self.assertIn("qux", tooltip)
        self.assertTrue(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

        # Fix the other mapping as well. No tooltip should be shown afterward.
        self.controller.load_mapping(second_input)
        gtk_iteration()
        self.controller.update_mapping(output_symbol="b")
        gtk_iteration()
        tooltip = status.get_tooltip_text()
        self.assertIsNone(tooltip)
        self.assertFalse(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

    def test_no_validation_tooltip_for_empty_mappings(self):
        self.controller.load_preset("preset1")
        self.throttle(20)

        status = self.user_interface.get("status_bar")
        self.assertIsNone(status.get_tooltip_text())

        self.controller.create_mapping()
        gtk_iteration()
        self.assertTrue(self.controller.is_empty_mapping())
        self.assertIsNone(status.get_tooltip_text())

    def test_check_macro_syntax(self):
        status = self.status_bar
        error_icon = self.user_interface.get("error_status_icon")
        warning_icon = self.user_interface.get("warning_status_icon")

        self.code_editor.get_buffer().set_text("k(1))")
        tooltip = status.get_tooltip_text().lower()
        self.assertIn("brackets", tooltip)
        self.assertTrue(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

        self.code_editor.get_buffer().set_text("k(1)")
        tooltip = (status.get_tooltip_text() or "").lower()
        self.assertNotIn("brackets", tooltip)
        self.assertFalse(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

        self.assertEqual(
            self.data_manager.active_mapping.output_symbol,
            "k(1)",
        )

    def test_check_on_typing(self):
        status = self.user_interface.get("status_bar")
        error_icon = self.user_interface.get("error_status_icon")
        warning_icon = self.user_interface.get("warning_status_icon")

        tooltip = status.get_tooltip_text()
        # nothing wrong yet
        self.assertIsNone(tooltip)

        # now change the mapping by typing into the field
        buffer = self.code_editor.get_buffer()
        buffer.set_text("sdfgkj()")
        gtk_iteration()

        # the mapping is validated
        tooltip = status.get_tooltip_text()
        self.assertIn("Unknown function sdfgkj", tooltip)
        self.assertTrue(error_icon.get_visible())
        self.assertFalse(warning_icon.get_visible())

        self.assertEqual(self.data_manager.active_mapping.output_symbol, "sdfgkj()")

    def test_select_device(self):
        # simple test to make sure we can switch between devices
        # more detailed tests in TestController and TestDataManager
        self.click_on_group("Bar Device")
        gtk_iteration()

        entries = {*FlowBoxTestUtils.get_child_names(self.preset_selection)}
        self.assertEqual(entries, {"new preset"})

        self.click_on_group("Foo Device")
        gtk_iteration()

        entries = {*FlowBoxTestUtils.get_child_names(self.preset_selection)}
        self.assertEqual(entries, {"preset1", "preset2", "preset3"})

        # make sure a preset and mapping was loaded
        self.assertIsNotNone(self.data_manager.active_preset)
        self.assertEqual(
            self.data_manager.active_preset.name,
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name,
        )
        self.assertIsNotNone(self.data_manager.active_mapping)
        self.assertEqual(
            self.data_manager.active_mapping.input_combination,
            self.selection_label_listbox.get_selected_row().combination,
        )

    def test_select_preset(self):
        # simple test to make sure we can switch between presets
        # more detailed tests in TestController and TestDataManager
        self.click_on_group("Foo Device 2")
        gtk_iteration()
        FlowBoxTestUtils.set_active(self.preset_selection, "preset1")
        gtk_iteration()

        mappings = {
            row.combination for row in self.selection_label_listbox.get_children()
        }
        self.assertEqual(
            mappings,
            {
                InputCombination([InputConfig(type=1, code=1)]),
                InputCombination([InputConfig(type=1, code=2)]),
            },
        )
        self.assertFalse(self.autoload_toggle.get_active())

        FlowBoxTestUtils.set_active(self.preset_selection, "preset2")
        gtk_iteration()

        mappings = {
            row.combination for row in self.selection_label_listbox.get_children()
        }
        self.assertEqual(
            mappings,
            {
                InputCombination([InputConfig(type=1, code=3)]),
                InputCombination([InputConfig(type=1, code=4)]),
            },
        )
        self.assertTrue(self.autoload_toggle.get_active())

    def test_copy_preset(self):
        # simple tests to ensure it works
        # more detailed tests in TestController and TestDataManager

        # check the initial state
        entries = {*FlowBoxTestUtils.get_child_names(self.preset_selection)}
        self.assertEqual(entries, {"preset1", "preset2", "preset3"})
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name, "preset3"
        )

        self.copy_preset_btn.clicked()
        gtk_iteration()
        entries = {*FlowBoxTestUtils.get_child_names(self.preset_selection)}
        self.assertEqual(entries, {"preset1", "preset2", "preset3", "preset3 copy"})
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name,
            "preset3 copy",
        )

        self.copy_preset_btn.clicked()
        gtk_iteration()

        entries = {*FlowBoxTestUtils.get_child_names(self.preset_selection)}
        self.assertEqual(
            entries, {"preset1", "preset2", "preset3", "preset3 copy", "preset3 copy 2"}
        )

    def test_wont_start(self):
        def wait():
            """Wait for the injector process to finish doing stuff."""
            for _ in range(10):
                time.sleep(0.1)
                gtk_iteration()
                if "Starting" not in self.get_status_text():
                    return

        error_icon = self.user_interface.get("error_status_icon")
        self.controller.load_group("Bar Device")

        # empty
        self.start_injector_btn.clicked()
        gtk_iteration()
        wait()
        text = self.get_status_text()
        self.assertIn("add mappings", text)
        self.assertTrue(error_icon.get_visible())
        self.assertNotEqual(self.daemon.get_state("Bar Device"), InjectorState.RUNNING)

        # device grabbing fails
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        for i in range(2):
            # just pressing apply again will overwrite the previous error
            self.grab_fails = True
            self.start_injector_btn.clicked()
            gtk_iteration()

            text = self.get_status_text()
            # it takes a little bit of time
            self.assertIn("Starting injection", text)
            self.assertFalse(error_icon.get_visible())
            wait()
            text = self.get_status_text()
            self.assertIn("Failed to apply preset", text)
            self.assertTrue(error_icon.get_visible())
            self.assertNotEqual(
                self.daemon.get_state("Foo Device 2"), InjectorState.RUNNING
            )

        # this time work properly

        self.grab_fails = False
        self.start_injector_btn.clicked()
        gtk_iteration()
        text = self.get_status_text()
        self.assertIn("Starting injection", text)
        self.assertFalse(error_icon.get_visible())
        wait()
        text = self.get_status_text()
        self.assertIn("Applied", text)
        text = self.get_status_text()
        self.assertNotIn("CTRL + DEL", text)  # only shown if btn_left mapped
        self.assertFalse(error_icon.get_visible())
        self.assertEqual(self.daemon.get_state("Foo Device 2"), InjectorState.RUNNING)

    def test_start_with_btn_left(self):
        self.controller.load_group("Foo Device 2")
        gtk_iteration()

        self.controller.create_mapping()
        gtk_iteration()
        self.controller.update_mapping(
            input_combination=InputCombination([InputConfig.btn_left()]),
            output_symbol="a",
        )
        gtk_iteration()

        def wait():
            """Wait for the injector process to finish doing stuff."""
            for _ in range(10):
                time.sleep(0.1)
                gtk_iteration()
                if "Starting" not in self.get_status_text():
                    return

        # first apply, shows btn_left warning
        self.start_injector_btn.clicked()
        gtk_iteration()
        text = self.get_status_text()
        self.assertIn("click", text)
        self.assertEqual(self.daemon.get_state("Foo Device 2"), InjectorState.UNKNOWN)

        # second apply, overwrites
        self.start_injector_btn.clicked()
        gtk_iteration()
        wait()
        self.assertEqual(self.daemon.get_state("Foo Device 2"), InjectorState.RUNNING)
        text = self.get_status_text()
        # because btn_left is mapped, shows help on how to stop
        # injecting via the keyboard
        self.assertIn("CTRL + DEL", text)

    def test_cannot_record_keys(self):
        self.controller.load_group("Foo Device 2")
        self.assertNotEqual(self.data_manager.get_state(), InjectorState.RUNNING)
        self.assertNotIn("Stop", self.get_status_text())

        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.assertTrue(self.recording_toggle.get_active())
        self.controller.stop_key_recording()
        gtk_iteration()
        self.assertFalse(self.recording_toggle.get_active())

        self.start_injector_btn.clicked()
        gtk_iteration()
        # wait for the injector to start
        for _ in range(10):
            time.sleep(0.1)
            gtk_iteration()
            if "Starting" not in self.get_status_text():
                break

        self.assertEqual(self.data_manager.get_state(), InjectorState.RUNNING)

        # the toggle button should reset itself shortly
        self.recording_toggle.set_active(True)
        gtk_iteration()
        self.assertFalse(self.recording_toggle.get_active())
        text = self.get_status_text()
        self.assertIn("Stop", text)

    def test_start_injecting(self):
        self.controller.load_group("Foo Device 2")

        with spy(self.daemon, "set_config_dir") as spy1:
            with spy(self.daemon, "start_injecting") as spy2:
                self.start_injector_btn.clicked()
                gtk_iteration()
                # correctly uses group.key, not group.name
                spy2.assert_called_once_with("Foo Device 2", "preset3")

            spy1.assert_called_once_with(PathUtils.get_config_path())

        for _ in range(10):
            time.sleep(0.1)
            gtk_iteration()
            if self.data_manager.get_state() == InjectorState.RUNNING:
                break

        # fail here so we don't block forever
        self.assertEqual(self.data_manager.get_state(), InjectorState.RUNNING)

        # this is a stupid workaround for the bad test fixtures
        # by switching the group we make sure that the reader-service no longer
        # listens for events on "Foo Device 2" otherwise we would have two processes
        # (reader-service and injector) reading the same pipe which can block this test
        # indefinitely
        self.controller.load_group("Foo Device")
        gtk_iteration()

        push_events(
            fixtures.foo_device_2_keyboard,
            [
                InputEvent.key(5, 1),
                InputEvent.key(5, 0),
            ],
        )

        event = uinput_write_history_pipe[0].recv()
        self.assertEqual(event.type, evdev.events.EV_KEY)
        self.assertEqual(event.code, KEY_A)
        self.assertEqual(event.value, 1)

        event = uinput_write_history_pipe[0].recv()
        self.assertEqual(event.type, evdev.events.EV_KEY)
        self.assertEqual(event.code, KEY_A)
        self.assertEqual(event.value, 0)

        # the input-remapper device will not be shown
        self.controller.refresh_groups()
        gtk_iteration()
        for child in self.device_selection.get_children():
            device_group_entry = child.get_children()[0]
            self.assertNotIn("input-remapper", device_group_entry.name)

    def test_stop_injecting(self):
        self.controller.load_group("Foo Device 2")
        self.start_injector_btn.clicked()
        gtk_iteration()

        for _ in range(10):
            time.sleep(0.1)
            gtk_iteration()
            if self.data_manager.get_state() == InjectorState.RUNNING:
                break

        # fail here so we don't block forever
        self.assertEqual(self.data_manager.get_state(), InjectorState.RUNNING)

        # stupid fixture workaround
        self.controller.load_group("Foo Device")
        gtk_iteration()

        pipe = uinput_write_history_pipe[0]
        self.assertFalse(pipe.poll())

        push_events(
            fixtures.foo_device_2_keyboard,
            [
                InputEvent.key(5, 1),
                InputEvent.key(5, 0),
            ],
        )

        time.sleep(0.2)
        self.assertTrue(pipe.poll())
        while pipe.poll():
            pipe.recv()

        self.controller.load_group("Foo Device 2")
        self.controller.stop_injecting()
        gtk_iteration()

        for _ in range(10):
            time.sleep(0.1)
            gtk_iteration()
            if self.data_manager.get_state() == InjectorState.STOPPED:
                break
        self.assertEqual(self.data_manager.get_state(), InjectorState.STOPPED)

        push_events(
            fixtures.foo_device_2_keyboard,
            [
                InputEvent.key(5, 1),
                InputEvent.key(5, 0),
            ],
        )
        time.sleep(0.2)
        self.assertFalse(pipe.poll())

    def test_delete_preset(self):
        # as per test_initial_state we already have preset3 loaded
        self.assertTrue(
            os.path.exists(PathUtils.get_preset_path("Foo Device", "preset3"))
        )

        with patch_confirm_delete(self.user_interface, Gtk.ResponseType.CANCEL):
            self.delete_preset_btn.clicked()
            gtk_iteration()
            self.assertTrue(
                os.path.exists(PathUtils.get_preset_path("Foo Device", "preset3"))
            )
            self.assertEqual(self.data_manager.active_preset.name, "preset3")
            self.assertEqual(self.data_manager.active_group.name, "Foo Device")

        with patch_confirm_delete(self.user_interface):
            self.delete_preset_btn.clicked()
            gtk_iteration()
            self.assertFalse(
                os.path.exists(PathUtils.get_preset_path("Foo Device", "preset3"))
            )
            self.assertEqual(self.data_manager.active_preset.name, "preset2")
            self.assertEqual(self.data_manager.active_group.name, "Foo Device")

    def test_refresh_groups(self):
        # sanity check: preset3 should be the newest
        self.assertEqual(
            FlowBoxTestUtils.get_active_entry(self.preset_selection).name, "preset3"
        )

        # select the older one
        FlowBoxTestUtils.set_active(self.preset_selection, "preset1")
        gtk_iteration()
        self.assertEqual(self.data_manager.active_preset.name, "preset1")

        # add a device that doesn't exist to the dropdown
        unknown_key = "key-1234"
        self.device_selection.insert(
            DeviceGroupEntry(self.message_broker, self.controller, None, unknown_key),
            0,
            # 0, [unknown_key, None, "foo"]
        )

        self.controller.refresh_groups()
        gtk_iteration()
        self.throttle(200)
        # the gui should not jump to a different preset suddenly
        self.assertEqual(self.data_manager.active_preset.name, "preset1")

        # just to verify that the mtime still tells us that preset3 is the newest one
        self.assertEqual(self.controller.get_a_preset(), "preset3")

        # the list contains correct entries
        # and the non-existing entry should be removed
        names = FlowBoxTestUtils.get_child_names(self.device_selection)
        icons = FlowBoxTestUtils.get_child_icons(self.device_selection)
        self.assertNotIn(unknown_key, names)

        self.assertIn("Foo Device", names)
        self.assertIn("Foo Device 2", names)
        self.assertIn("Bar Device", names)
        self.assertIn("gamepad", names)

        self.assertIn("input-keyboard", icons)
        self.assertIn("input-gaming", icons)
        self.assertIn("input-keyboard", icons)
        self.assertIn("input-gaming", icons)

        # it won't crash due to "list index out of range"
        # when `types` is an empty list. Won't show an icon
        self.data_manager._reader_client.groups.find(key="Foo Device 2").types = []
        self.data_manager._reader_client.publish_groups()
        gtk_iteration()
        self.assertIn(
            "Foo Device 2",
            FlowBoxTestUtils.get_child_names(self.device_selection),
        )

    def test_shared_presets(self):
        # devices with the same name (but different key because the key is
        # unique) share the same presets.
        # Those devices would usually be of the same model of keyboard for example
        # Todo: move this to unit tests, there is no point in having the ui around
        self.controller.load_group("Foo Device")
        presets1 = self.data_manager.get_preset_names()
        self.controller.load_group("Foo Device 2")
        gtk_iteration()
        presets2 = self.data_manager.get_preset_names()
        self.controller.load_group("Bar Device")
        gtk_iteration()
        presets3 = self.data_manager.get_preset_names()

        self.assertEqual(presets1, presets2)
        self.assertNotEqual(presets1, presets3)

    def test_delete_last_preset(self):
        with patch_confirm_delete(self.user_interface):
            # as per test_initial_state we already have preset3 loaded
            self.assertEqual(self.data_manager.active_preset.name, "preset3")

            self.delete_preset_btn.clicked()
            gtk_iteration()
            # the next newest preset should be loaded
            self.assertEqual(self.data_manager.active_preset.name, "preset2")
            self.delete_preset_btn.clicked()
            gtk_iteration()
            self.delete_preset_btn.clicked()
            # the ui should be clean
            self.assert_gui_clean()
            device_path = f"{PathUtils.config_path()}/presets/{self.data_manager.active_group.name}"
            self.assertTrue(os.path.exists(f"{device_path}/new preset.json"))

            self.delete_preset_btn.clicked()
            gtk_iteration()
            # deleting an empty preset als doesn't do weird stuff
            self.assert_gui_clean()
            device_path = f"{PathUtils.config_path()}/presets/{self.data_manager.active_group.name}"
            self.assertTrue(os.path.exists(f"{device_path}/new preset.json"))

    def test_enable_disable_output(self):
        # load a group without any presets
        self.controller.load_group("Bar Device")

        # should be disabled by default since no key is recorded yet
        self.assertEqual(self.get_unfiltered_symbol_input_text(), SET_KEY_FIRST)
        self.assertFalse(self.output_box.get_sensitive())

        # create a mapping
        self.controller.create_mapping()
        gtk_iteration()

        # should still be disabled
        self.assertEqual(self.get_unfiltered_symbol_input_text(), SET_KEY_FIRST)
        self.assertFalse(self.output_box.get_sensitive())

        # enable it by sending a combination
        self.controller.start_key_recording()
        gtk_iteration()
        push_events(
            fixtures.bar_device,
            [
                InputEvent(0, 0, 1, 30, 1),
                InputEvent(0, 0, 1, 30, 0),
            ],
        )
        self.throttle(100)  # give time for the input to arrive

        self.assertEqual(
            self.get_unfiltered_symbol_input_text(), CodeEditor.placeholder
        )
        self.assertTrue(self.output_box.get_sensitive())

        # disable it by deleting the mapping
        with patch_confirm_delete(self.user_interface):
            self.delete_mapping_btn.clicked()
            gtk_iteration()

        self.assertEqual(self.get_unfiltered_symbol_input_text(), SET_KEY_FIRST)
        self.assertFalse(self.output_box.get_sensitive())


@test_setup
class TestAutocompletion(GuiTestBase):
    def press_key(self, keyval):
        event = Gdk.EventKey()
        event.keyval = keyval
        self.user_interface.autocompletion.navigate(None, event)

    def get_suggestions(self, autocompletion):
        return [
            row.get_children()[0].get_text()
            for row in autocompletion.list_box.get_children()
        ]

    def test_get_incomplete_parameter(self):
        def test(text, expected):
            text_view = Gtk.TextView()
            Gtk.TextView.do_insert_at_cursor(text_view, text)
            text_iter = text_view.get_iter_at_location(0, 0)[1]
            text_iter.set_offset(len(text))
            self.assertEqual(get_incomplete_parameter(text_iter), expected)

        test("bar(foo", "foo")
        test("bar(a=foo", "foo")
        test("bar(qux, foo", "foo")
        test("foo", "foo")
        test("bar + foo", "foo")

    def test_get_incomplete_function_name(self):
        def test(text, expected):
            text_view = Gtk.TextView()
            Gtk.TextView.do_insert_at_cursor(text_view, text)
            text_iter = text_view.get_iter_at_location(0, 0)[1]
            text_iter.set_offset(len(text))
            self.assertEqual(get_incomplete_function_name(text_iter), expected)

        test("bar().foo", "foo")
        test("bar()\n.foo", "foo")
        test("bar().\nfoo", "foo")
        test("bar(\nfoo", "foo")
        test("bar(\nqux=foo", "foo")
        test("bar(KEY_A,\nfoo", "foo")
        test("foo", "foo")

    def test_autocomplete_names(self):
        autocompletion = self.user_interface.autocompletion

        def setup(text):
            self.set_focus(self.code_editor)
            self.code_editor.get_buffer().set_text("")
            Gtk.TextView.do_insert_at_cursor(self.code_editor, text)
            self.throttle(200)
            text_iter = self.code_editor.get_iter_at_location(0, 0)[1]
            text_iter.set_offset(len(text))

        setup("disa")
        self.assertNotIn("KEY_A", self.get_suggestions(autocompletion))
        self.assertIn("disable", self.get_suggestions(autocompletion))

        setup(" + _A")
        self.assertIn("KEY_A", self.get_suggestions(autocompletion))
        self.assertNotIn("disable", self.get_suggestions(autocompletion))

    def test_autocomplete_key(self):
        self.controller.update_mapping(output_symbol="")
        gtk_iteration()

        self.set_focus(self.code_editor)
        self.code_editor.get_buffer().set_text("")

        complete_key_name = "Test_Foo_Bar"

        keyboard_layout.clear()
        keyboard_layout._set(complete_key_name, 1)
        keyboard_layout._set("KEY_A", 30)  # we need this for the UIMapping to work

        # it can autocomplete a combination inbetween other things
        incomplete = "qux_1\n +  + qux_2"
        Gtk.TextView.do_insert_at_cursor(self.code_editor, incomplete)
        Gtk.TextView.do_move_cursor(
            self.code_editor,
            Gtk.MovementStep.VISUAL_POSITIONS,
            -8,
            False,
        )

        Gtk.TextView.do_insert_at_cursor(self.code_editor, "foo")
        self.throttle(200)
        gtk_iteration()

        autocompletion = self.user_interface.autocompletion
        self.assertTrue(autocompletion.visible)

        self.press_key(Gdk.KEY_Down)
        self.press_key(Gdk.KEY_Return)
        self.throttle(200)
        gtk_iteration()

        # the first suggestion should have been selected

        modified_symbol = self.get_code_input()
        self.assertEqual(modified_symbol, f"qux_1\n + {complete_key_name} + qux_2")

        # try again, but a whitespace completes the word and so no autocompletion
        # should be shown
        Gtk.TextView.do_insert_at_cursor(self.code_editor, " + foo ")

        time.sleep(0.11)
        gtk_iteration()

        self.assertFalse(autocompletion.visible)

    def test_autocomplete_function(self):
        self.controller.update_mapping(output_symbol="")
        gtk_iteration()

        source_view = self.focus_source_view()

        incomplete = "key(KEY_A).\nepea"
        Gtk.TextView.do_insert_at_cursor(source_view, incomplete)

        time.sleep(0.11)
        gtk_iteration()

        autocompletion = self.user_interface.autocompletion
        self.assertTrue(autocompletion.visible)

        self.press_key(Gdk.KEY_Down)
        self.press_key(Gdk.KEY_Return)

        # the first suggestion should have been selected
        modified_symbol = self.get_code_input()
        self.assertEqual(modified_symbol, "key(KEY_A).\nrepeat")

    def test_close_autocompletion(self):
        self.controller.update_mapping(output_symbol="")
        gtk_iteration()

        source_view = self.focus_source_view()

        Gtk.TextView.do_insert_at_cursor(source_view, "KEY_")

        time.sleep(0.11)
        gtk_iteration()

        autocompletion = self.user_interface.autocompletion
        self.assertTrue(autocompletion.visible)

        self.press_key(Gdk.KEY_Down)
        self.press_key(Gdk.KEY_Escape)

        self.assertFalse(autocompletion.visible)

        symbol = self.get_code_input()
        self.assertEqual(symbol, "KEY_")

    def test_writing_still_works(self):
        self.controller.update_mapping(output_symbol="")
        gtk_iteration()
        source_view = self.focus_source_view()

        Gtk.TextView.do_insert_at_cursor(source_view, "KEY_")

        autocompletion = self.user_interface.autocompletion

        time.sleep(0.11)
        gtk_iteration()
        self.assertTrue(autocompletion.visible)

        # writing still works while an entry is selected
        self.press_key(Gdk.KEY_Down)

        Gtk.TextView.do_insert_at_cursor(source_view, "A")

        time.sleep(0.11)
        gtk_iteration()
        self.assertTrue(autocompletion.visible)

        Gtk.TextView.do_insert_at_cursor(source_view, "1234foobar")

        time.sleep(0.11)
        gtk_iteration()
        # no key matches this completion, so it closes again
        self.assertFalse(autocompletion.visible)

    def test_cycling(self):
        self.controller.update_mapping(output_symbol="")
        gtk_iteration()
        source_view = self.focus_source_view()

        Gtk.TextView.do_insert_at_cursor(source_view, "KEY_")

        autocompletion = self.user_interface.autocompletion

        time.sleep(0.11)
        gtk_iteration()
        self.assertTrue(autocompletion.visible)

        self.assertEqual(
            autocompletion.scrolled_window.get_vadjustment().get_value(), 0
        )

        # cycle to the end of the list because there is no element higher than index 0
        self.press_key(Gdk.KEY_Up)
        self.assertGreater(
            autocompletion.scrolled_window.get_vadjustment().get_value(), 0
        )

        # go back to the start, because it can't go down further
        self.press_key(Gdk.KEY_Down)
        self.assertEqual(
            autocompletion.scrolled_window.get_vadjustment().get_value(), 0
        )


@test_setup
class TestDebounce(unittest.TestCase):
    def test_debounce(self):
        calls = 0

        class A:
            @debounce(20)
            def foo(self):
                nonlocal calls
                calls += 1

        # two methods with the same name don't confuse debounce
        class B:
            @debounce(20)
            def foo(self):
                nonlocal calls
                calls += 1

        a = A()
        b = B()

        self.assertEqual(calls, 0)

        a.foo()
        gtk_iteration()
        self.assertEqual(calls, 0)

        b.foo()
        gtk_iteration()
        self.assertEqual(calls, 0)

        time.sleep(0.021)
        gtk_iteration()
        self.assertEqual(calls, 2)

        a.foo()
        b.foo()
        a.foo()
        b.foo()
        gtk_iteration()
        self.assertEqual(calls, 2)

        time.sleep(0.021)
        gtk_iteration()
        self.assertEqual(calls, 4)

    def test_run_all_now(self):
        calls = 0

        class A:
            @debounce(20)
            def foo(self):
                nonlocal calls
                calls += 1

        a = A()
        a.foo()
        gtk_iteration()
        self.assertEqual(calls, 0)

        debounce_manager.run_all_now()
        self.assertEqual(calls, 1)

        # waiting for some time will not call it again
        time.sleep(0.021)
        gtk_iteration()
        self.assertEqual(calls, 1)

    def test_stop_all(self):
        calls = 0

        class A:
            @debounce(20)
            def foo(self):
                nonlocal calls
                calls += 1

        a = A()
        a.foo()
        gtk_iteration()
        self.assertEqual(calls, 0)

        debounce_manager.stop_all()

        # waiting for some time will not call it
        time.sleep(0.021)
        gtk_iteration()
        self.assertEqual(calls, 0)

    def test_stop(self):
        calls = 0

        class A:
            @debounce(20)
            def foo(self):
                nonlocal calls
                calls += 1

        a = A()
        a.foo()
        gtk_iteration()
        self.assertEqual(calls, 0)

        debounce_manager.stop(a, a.foo)

        # waiting for some time will not call it
        time.sleep(0.021)
        gtk_iteration()
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
