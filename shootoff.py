#!/usr/bin/env python2

# Copyright (c) 2013 phrack. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from canvas_manager import CanvasManager
import configurator
from configurator import Configurator
import cv2
import glob
import imp
import numpy
import os
from PIL import Image, ImageTk
from preferences_editor import PreferencesEditor
import re
from shot import Shot
from tag_parser import TagParser
from target_editor import TargetEditor
from target_pickler import TargetPickler
import time
from training_protocols.protocol_operations import ProtocolOperations
from threading import Thread
import Tkinter, tkFileDialog, tkMessageBox, ttk

FEED_FPS = 30  # ms
SHOT_MARKER = "shot_marker"
TARGET_VISIBILTY_MENU_INDEX = 3

DEFAULT_SHOT_LIST_COLUMNS = ("Time", "Laser")


class MainWindow:
    def refresh_frame(self, *args):
        rval, self._webcam_frame = self._cv.read()

        if (rval == False):
            self._refresh_miss_count += 1
            self.logger.debug ("Missed %d webcam frames. If we miss too many ShootOFF " +
                "will stop processing shots.", self._refresh_miss_count)

            if self._refresh_miss_count >= 25:
                tkMessageBox.showerror("Webcam Disconnected", "Missed too many " +
                    "webcam frames. The camera is probably disconnected so " +
                    "ShootOFF will stop processing shots.")
                self.logger.critical("Missed %d webcam frames. The camera is probably " +
                    "disconnected so ShootOFF will stop processing shots.",
                    self._refresh_miss_count)
                self._shutdown = True
            else:
                if self._shutdown == False:
                    self._window.after(FEED_FPS, self.refresh_frame)

            return

        self._refresh_miss_count = 0

        #OpenCV reads the frame in BGR, but PIL uses RGB, so we if we don't
        #convert it, the colors will be off.
        webcam_image = cv2.cvtColor(self._webcam_frame, cv2.cv.CV_BGR2RGB)

        # If the shot detector saw interference, we need to show it now
        if self._show_interference:
            if self._interference_iterations > 0:
                self._interference_iterations -= 1

                frame_bw = cv2.cvtColor(self._webcam_frame, cv2.cv.CV_BGR2GRAY)
                (thresh, webcam_image) = cv2.threshold(frame_bw,
                    self._preferences[configurator.LASER_INTENSITY], 255,
                    cv2.THRESH_BINARY)

        # Show webcam image a Tk image container (note:
        # if the image isn't stored in an instance variable
        # it will be garbage collected and not show)
        self._image = ImageTk.PhotoImage(image=Image.fromarray(webcam_image))

        # If the target editor doesn't have its own copy of the image
        # the webcam feed will never update again after the editor opens
        self._editor_image = ImageTk.PhotoImage(
            image=Image.fromarray(webcam_image))

        webcam_image = self._webcam_canvas.create_image(0, 0, image=self._image,
            anchor=Tkinter.NW, tags=("background"))

        # Drawing the new frame covers up our targets, so
        # move it to the back if they are supposed to show
        if self._show_targets:
            # Not raising existing targets while lowering the webcam feed
            # will cause hits to stop registering on targets
            for target in self._targets:
                self._webcam_canvas.tag_raise(target)
            self._webcam_canvas.tag_raise(SHOT_MARKER)
            self._webcam_canvas.tag_lower(webcam_image)
        else:
            # We have to lower canvas then the targets so
            # that anything drawn by plugins will still show
            # but the targets won't
            self._webcam_canvas.tag_raise(SHOT_MARKER)
            self._webcam_canvas.tag_lower(webcam_image)
            for target in self._targets:
                self._webcam_canvas.tag_lower(target)

        if self._shutdown == False:
            self._window.after(FEED_FPS, self.refresh_frame)

    def detect_shots(self):
        if (self._webcam_frame is None):
            self._window.after(self._preferences[configurator.DETECTION_RATE], self.detect_shots)
            return

        # Makes feed black and white
        frame_bw = cv2.cvtColor(self._webcam_frame, cv2.cv.CV_BGR2GRAY)

        # Threshold the image
        (thresh, frame_thresh) = cv2.threshold(frame_bw, 
            self._preferences[configurator.LASER_INTENSITY], 255, cv2.THRESH_BINARY)

        # Determine if we have a light source or glare on the feed
        if not self._seen_interference:
            self.detect_interfence(frame_thresh)

        # Find min and max values on the black and white frame
        min_max = cv2.minMaxLoc(frame_thresh)

        # The minimum and maximum are the same if there was
        # nothing detected
        if (min_max[0] != min_max[1]):
            x = min_max[3][0]
            y = min_max[3][1]

            laser_color = self.detect_laser_color(x, y)

            # If we couldn't detect a laser color, it's probably not a
            # shot
            if (laser_color is not None and
                self._preferences[configurator.IGNORE_LASER_COLOR] not in laser_color):

                self.handle_shot(laser_color, x, y)

        if self._shutdown == False:
            self._window.after(self._preferences[configurator.DETECTION_RATE],
                self.detect_shots)

    def handle_shot(self, laser_color, x, y):
        timestamp = 0

        # Start the shot timer if it has not been started yet,
        # otherwise get the time offset
        if self._shot_timer_start is None:
            self._shot_timer_start = time.time()
        else:
            timestamp = time.time() - self._shot_timer_start

        tree_item = None

        if "green" in laser_color:
            tree_item = self._shot_timer_tree.insert("", "end",
                values=[timestamp, "green"])
        else:
            tree_item = self._shot_timer_tree.insert("", "end",
                values=[timestamp, laser_color])
        self._shot_timer_tree.see(tree_item)

        new_shot = Shot((x, y), self._webcam_canvas,
            self._preferences[configurator.MARKER_RADIUS],
            laser_color, timestamp)
        self._shots.append(new_shot)
        new_shot.draw_marker()

        # Process the shot to see if we hit a region and perform
        # a training protocol specific action and any if we did
        # command tag actions if we did
        self.process_hit(new_shot, tree_item)

    def detect_interfence(self, image_thresh):
        brightness_hist = cv2.calcHist([image_thresh], [0], None, [256], [0, 255])
        percent_dark = brightness_hist[0] / image_thresh.size

        # If 99% of thresholded image isn't dark, we probably have
        # a light source or glare in the image
        if (percent_dark < .99):
            # We will only warn about interference once each run
            self._seen_interference = True

            self.logger.warning(
                "Glare or light source detected. %f of the image is dark." %
                percent_dark)

            self._show_interference = tkMessageBox.askyesno("Interference Detected", "Bright glare or a light source has been detected on the webcam feed, which will interfere with shot detection. Do you want to see a feed where the interference will be white and everything else will be black for a short period of time?")

            if self._show_interference:
                # calculate the number of times we should show the
                # interference image (this should be roughly 5 seconds)
                self._interference_iterations = 2500 / FEED_FPS

    def detect_laser_color(self, x, y):
        # Get the average color around the coordinates. If
        # the dominant color is red, it's a red laser, if
        # it's green it's a green laser, otherwise it's probably
        # not a laser trainer, so ignore it
        l = self._webcam_frame.shape[1]
        h = self._webcam_frame.shape[0]
        mask = numpy.zeros((h, l, 1), numpy.uint8)
        cv2.circle(mask, (x, y), 10, (255, 255, 555), -1)
        mean_color = cv2.mean(self._webcam_frame, mask)

        # Remember that self._webcam_frame is in BGR
        r = mean_color[2]
        g = mean_color[1]
        b = mean_color[0]

        if (r > g) and (r > b):
            return "red"

        if (g > r) and (g > b):
            return "green2"

        return None

    def process_hit(self, shot, shot_list_item):
        is_hit = False

        x = shot.get_coords()[0]
        y = shot.get_coords()[1]

        regions = self._webcam_canvas.find_overlapping(x, y, x, y)

        # If we hit a targert region, run its commands and notify the
        # loaded plugin of the hit
        for region in reversed(regions):
            tags = TagParser.parse_tags(
                self._webcam_canvas.gettags(region))

            if "_internal_name" in tags and "command" in tags:
                self.execute_region_commands(tags["command"])

            if "_internal_name" in tags and self._loaded_training != None:
                self._loaded_training.hit_listener(region, tags, shot, shot_list_item)

            if "_internal_name" in tags:
                is_hit = True
                # only run the commands and notify a hit for the top most
                # region
                break

        if self._loaded_training != None:
            self._loaded_training.shot_listener(shot, shot_list_item, is_hit)

    def open_target_editor(self):
        TargetEditor(self._frame, self._editor_image,
                     notifynewfunc=self.new_target_listener)

    def add_target(self, name):
        # The target count is just supposed to prevent target naming collisions,
        # not keep track of how many active targets there are
        target_name = "_internal_name:target" + str(self._target_count)
        self._target_count += 1

        target_pickler = TargetPickler()
        (region_object, regions) = target_pickler.load(
            name, self._webcam_canvas, target_name)

        self._targets.append(target_name)

    def edit_target(self, name):
        TargetEditor(self._frame, self._editor_image, name,
                     self.new_target_listener)

    def new_target_listener(self, target_file):
        (root, ext) = os.path.splitext(os.path.basename(target_file))
        self._add_target_menu.add_command(label=root,
                command=self.callback_factory(self.add_target,
                target_file))
        self._edit_target_menu.add_command(label=root,
                command=self.callback_factory(self.edit_target,
                target_file))

    def execute_region_commands(self, command_list):
        args = []

        for command in command_list:
            # Parse the command name and arguments arguments are expected to
            # be comma separated and in between paren:
            # command_name(arg0,arg1,...,argN)
            pattern = r'(\w[\w\d_]*)\((.*)\)$'
            match = re.match(pattern, command)
            if match:
                command = match.groups()[0]
                if len(match.groups()) > 0:
                    args = match.groups()[1].split(",")

            # Run the commands
            if command == "clear_shots":
                self.clear_shots()

            if command == "play_sound":
                self._protocol_operations.play_sound(args[0])

    def toggle_target_visibility(self):
        if self._show_targets:
            self._targets_menu.entryconfig(TARGET_VISIBILTY_MENU_INDEX,
                label="Show Targets")
        else:
            self._targets_menu.entryconfig(TARGET_VISIBILTY_MENU_INDEX,
                label="Hide Targets")

        self._show_targets = not self._show_targets

    def clear_shots(self):
        self._webcam_canvas.delete(SHOT_MARKER)
        self._shots = []

        if self._loaded_training != None:
            self._loaded_training.reset(self.aggregate_targets())

        self._shot_timer_start = None
        shot_entries = self._shot_timer_tree.get_children()
        for shot in shot_entries: self._shot_timer_tree.delete(shot)
        self._previous_shot_time_selection = None

        self._webcam_canvas.focus_set()

    def quit(self):
        self._shutdown = True
        self._cv.release()
        self._window.quit()

    def canvas_click_red(self, event):
        if self._preferences[configurator.DEBUG]:
            self.handle_shot("red", event.x, event.y)

    def canvas_click_green(self, event):
        if self._preferences[configurator.DEBUG]:
            self.handle_shot("green", event.x, event.y)

    def canvas_click(self, event):
        # find the target that was selected
        # if a target wasn't clicked, _selected_target
        # will be empty and all targets will be dim
        selected_region = event.widget.find_closest(
            event.x, event.y)
        target_name = ""

        for tag in self._webcam_canvas.gettags(selected_region):
            if tag.startswith("_internal_name:"):
                target_name = tag
                break

        if self._selected_target == target_name:
            return

        self._canvas_manager.selection_update_listener(self._selected_target,
                                                       target_name)
        self._selected_target = target_name

    def canvas_delete_target(self, event):
        if (self._selected_target):
            for target in self._targets:
                if target == self._selected_target:
                    self._targets.remove(target)
            event.widget.delete(self._selected_target)
            self._selected_target = ""

    def cancel_training(self):
        if self._loaded_training:
            self._loaded_training.destroy()
            self._protocol_operations.destroy()
            self._loaded_training = None

    def aggregate_targets(self):
        # Create a list of targets, their regions, and the tags attached
        # to those regions so that the plugin can have a stock of what
        # can be shot
        targets = []

        for target in self._targets:
            target_regions = self._webcam_canvas.find_withtag(target)
            target_data = {"name": target, "regions": []}
            targets.append(target_data)

            for region in target_regions:
                tags = TagParser.parse_tags(
                    self._webcam_canvas.gettags(region))
                target_data["regions"].append(tags)

        return targets

    def load_training(self, plugin):
        targets = self.aggregate_targets()

        if self._loaded_training:
            self._loaded_training.destroy()

        if self._protocol_operations:
            self._protocol_operations.destroy()

        self._protocol_operations = ProtocolOperations(self._webcam_canvas, self)
        self._loaded_training = imp.load_module("__init__", *plugin).load(
            self._protocol_operations, targets)

    def edit_preferences(self):
        preferences_editor = PreferencesEditor(self._window, self._config_parser,
                                               self._preferences)

    def which(self, program):
        def is_exe(fpath):
            return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

        fpath, fname = os.path.split(program)
        if fpath:
            if is_exe(program):
                return program
        else:
            for path in os.environ["PATH"].split(os.pathsep):
                path = path.strip('"')
                exe_file = os.path.join(path, program)
                if is_exe(exe_file):
                    return exe_file

        return None

    def save_feed_image(self):
        # If ghostscript is not installed, you can only save as an EPS.
        # This is because of the way images are saved (see below).
        filetypes = []

        if (self.which("gs") is None and self.which("gswin32c.exe") is None
            and self.which("gswin64c.exe") is None):
            filetypes=[("Encapsulated PostScript", "*.eps")]
        else:
           filetypes=[("Portable Network Graphics", "*.png"),
                ("Encapsulated PostScript", "*.eps"),
                ("GIF", "*.gif"), ("JPEG", "*.jpeg")]

        image_file = tkFileDialog.asksaveasfilename(
            filetypes=filetypes,
            title="Save ShootOFF Webcam Feed",
            parent=self._window)

        if not image_file: return

        file_name, extension = os.path.splitext(image_file)

        # The Tkinter canvas only supports saving its contents in postscript,
        # so if the user wanted something different we should convert the
        # postscript file using PIL then delete the temporary postscript file.
        # PIL can only open an eps file is Ghostscript is installed.
        if ".eps" not in extension:
            self._webcam_canvas.postscript(file=(file_name + "tmp.eps"))
            img = Image.open(file_name + "tmp.eps", "r")
            img.save(image_file, extension[1:])
            del img
            os.remove(file_name + "tmp.eps")
        else:
            self._webcam_canvas.postscript(file=(file_name + ".eps"))

    def shot_time_selected(self, event):
        selected_shots = event.widget.focus()
        shot_index = event.widget.index(selected_shots)
        self._shots[shot_index].toggle_selected()

        if self._previous_shot_time_selection is not None:
            self._previous_shot_time_selection.toggle_selected()

        self._previous_shot_time_selection = self._shots[shot_index]

        self._webcam_canvas.focus_set()

    def configure_default_shot_list_columns(self):
        self.configure_shot_list_columns(DEFAULT_SHOT_LIST_COLUMNS, [50, 50])

    def add_shot_list_columns(self, id_list):
        current_columns = self._shot_timer_tree.cget("columns")
        if not current_columns:
            self._shot_timer_tree.configure(columns=(id_list))
        else:
            self._shot_timer_tree.configure(columns=(current_columns + id_list))

    def resize_shot_list(self):
        self._shot_timer_tree.configure(displaycolumns="#all")

    # This method removes all but the default columns for the shot list
    def revert_shot_list_columns(self):
        self._shot_timer_tree.configure(columns=DEFAULT_SHOT_LIST_COLUMNS)
        self.configure_default_shot_list_columns()

        shot_entries = self._shot_timer_tree.get_children()
        for shot in shot_entries:
            current_values = self._shot_timer_tree.item(shot, "values")
            default_values = current_values[0:len(DEFAULT_SHOT_LIST_COLUMNS)]
            self._shot_timer_tree.item(shot, values=default_values)

        self.resize_shot_list()

    def configure_shot_list_columns(self, names, widths):
        for name, width in zip(names, widths):
            self.configure_shot_list_column(name, width)

        self.resize_shot_list()

    def append_shot_list_column_data(self, item, values):
        current_values = self._shot_timer_tree.item(item, "values")
        self._shot_timer_tree.item(item, values=(current_values + values))

    def configure_shot_list_column(self, name, width):
        self._shot_timer_tree.heading(name, text=name)
        self._shot_timer_tree.column(name, width=width, stretch=False)

    def build_gui(self, feed_dimensions=(600, 480)):
        # Create the main window
        self._window = Tkinter.Tk()
        self._window.protocol("WM_DELETE_WINDOW", self.quit)
        self._window.title("ShootOFF")

        self._frame = ttk.Frame(self._window)
        self._frame.pack()

        # Create the container for our webcam image
        self._webcam_canvas = Tkinter.Canvas(self._frame,
            width=feed_dimensions[0], height=feed_dimensions[1])
        self._webcam_canvas.grid(row=0, column=0)

        self._webcam_canvas.bind('<ButtonPress-1>', self.canvas_click)
        self._webcam_canvas.bind('<Delete>', self.canvas_delete_target)
        # Click to shoot
        if self._preferences[configurator.DEBUG]:
            self._webcam_canvas.bind('<Shift-ButtonPress-1>', self.canvas_click_red)
            self._webcam_canvas.bind('<Control-ButtonPress-1>', self.canvas_click_green)

        self._canvas_manager = CanvasManager(self._webcam_canvas)

        # Create a button to clear shots
        self._clear_shots_button = ttk.Button(
            self._frame, text="Clear Shots", command=self.clear_shots)
        self._clear_shots_button.grid(row=1, column=0)

        # Create the shot timer tree
        self._shot_timer_tree = ttk.Treeview(self._frame, selectmode="browse",
                                             show="headings")
        self.add_shot_list_columns(DEFAULT_SHOT_LIST_COLUMNS)
        self.configure_default_shot_list_columns()

        tree_scrolly = ttk.Scrollbar(self._frame, orient=Tkinter.VERTICAL,
                                     command=self._shot_timer_tree.yview)
        self._shot_timer_tree['yscroll'] = tree_scrolly.set

        tree_scrollx = ttk.Scrollbar(self._frame, orient=Tkinter.HORIZONTAL,
                                     command=self._shot_timer_tree.xview)
        self._shot_timer_tree['xscroll'] = tree_scrollx.set

        self._shot_timer_tree.grid(row=0, column=1, rowspan=2, sticky=Tkinter.NSEW)
        tree_scrolly.grid(row=0, column=2, rowspan=2, stick=Tkinter.NS)
        tree_scrollx.grid(row=1, column=1, stick=Tkinter.EW)
        self._shot_timer_tree.bind("<<TreeviewSelect>>", self.shot_time_selected)

        self.create_menu()

    def create_menu(self):
        menu_bar = Tkinter.Menu(self._window)
        self._window.config(menu=menu_bar)

        file_menu = Tkinter.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Preferences", command=self.edit_preferences)
        file_menu.add_command(label="Save Feed Image...", command=self.save_feed_image)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menu_bar.add_cascade(label="File", menu=file_menu)

        # Update TOGGLE_VISIBILTY_INDEX if another command is added
        # before the high targets command
        self._targets_menu = Tkinter.Menu(menu_bar, tearoff=False)
        self._targets_menu.add_command(label="Create Target...",
            command=self.open_target_editor)
        self._add_target_menu = self.create_target_list_menu(
            self._targets_menu, "Add Target", self.add_target)
        self._edit_target_menu = self.create_target_list_menu(
            self._targets_menu, "Edit Target", self.edit_target)
        self._targets_menu.add_command(label="Hide Targets",
            command=self.toggle_target_visibility)
        menu_bar.add_cascade(label="Targets", menu=self._targets_menu)

        training_menu = Tkinter.Menu(menu_bar, tearoff=False)

        # Add none button to turn off training protocol
        self._training_selection = Tkinter.StringVar()
        name = "None"
        training_menu.add_radiobutton(label=name, command=self.cancel_training,
                variable=self._training_selection, value=name)
        self._training_selection.set(name)

        self.create_training_list(training_menu, self.load_training)
        menu_bar.add_cascade(label="Training", menu=training_menu)

    def callback_factory(self, func, name):
        return lambda: func(name)

    def create_target_list_menu(self, menu, name, func):
        targets = glob.glob("targets/*.target")

        target_list_menu = Tkinter.Menu(menu, tearoff=False)

        for target in targets:
            (root, ext) = os.path.splitext(os.path.basename(target))
            target_list_menu.add_command(label=root,
                command=self.callback_factory(func, target))

        menu.add_cascade(label=name, menu=target_list_menu)

        return target_list_menu

    def create_training_list(self, menu, func):
        protocols_dir = "training_protocols"

        plugin_candidates = os.listdir(protocols_dir)
        for candidate in plugin_candidates:
            plugin_location = os.path.join(protocols_dir, candidate)
            if (not os.path.isdir(plugin_location) or
                not "__init__.py" in os.listdir(plugin_location)):
                continue
            plugin_info = imp.find_module("__init__", [plugin_location])
            training_info = imp.load_module("__init__", *plugin_info).get_info()
            menu.add_radiobutton(label=training_info["name"],
                command=self.callback_factory(self.load_training, plugin_info),
                variable=self._training_selection, value=training_info["name"])

    def __init__(self, config):
        self._shots = []
        self._targets = []
        self._target_count = 0
        self._refresh_miss_count = 0
        self._show_targets = True
        self._selected_target = ""
        self._loaded_training = None
        self._seen_interference = False
        self._show_interference = False
        self._webcam_frame = None
        self._config_parser = config.get_config_parser()
        self._preferences = config.get_preferences()
        self._shot_timer_start = None
        self._previous_shot_time_selection = None
        self.logger = config.get_logger()

        self._cv = cv2.VideoCapture(0)

        if self._cv.isOpened():
            width = self._cv.get(cv2.cv.CV_CAP_PROP_FRAME_WIDTH)
            height = self._cv.get(cv2.cv.CV_CAP_PROP_FRAME_HEIGHT)

            # If the resolution is too low, try to force it higher.
            # Some users have drivers that default to extremely low
            # resolutions and opencv doesn't currently make it easy
            # to enumerate valid resolutions and switch to them
            if width < 640 and height < 480:
                self.logger.info("Webcam resolution is current low (%dx%d), " +
                                 "attempting to increase it to 640x480", width, height)
                self._cv.set(cv2.cv.CV_CAP_PROP_FRAME_WIDTH, 640)
                self._cv.set(cv2.cv.CV_CAP_PROP_FRAME_HEIGHT, 480)
                width = self._cv.get(cv2.cv.CV_CAP_PROP_FRAME_WIDTH)
                height = self._cv.get(cv2.cv.CV_CAP_PROP_FRAME_HEIGHT)

            self.logger.debug("Webcam resolution is %dx%d", width, height)
            self.build_gui((width, height))
            self._protocol_operations = ProtocolOperations(self._webcam_canvas, self)

            fps = self._cv.get(cv2.cv.CV_CAP_PROP_FPS)
            if fps <= 0:
                self.logger.info("Couldn't get webcam FPS, defaulting to 30.")
            else:
                FEED_FPS = fps
                self.logger.info("Feed FPS set to %d.", fps)

            # Webcam related threads will end when this is true
            self._shutdown = False

        else:
            tkMessageBox.showerror("Couldn't Connect to Webcam", "Video capturing " +
                "could not be initialized either because there is no webcam or " +
                "we cannot connect to it. ShootOFF will shut down.")
            self.logger.critical("Video capturing could not be initialized either " +
                "because there is no webcam or we cannot connect to it.")
            self._shutdown = True

    def main(self):
        #Start the refresh loop that shows the webcam feed
        self._refresh_thread = Thread(target=self.refresh_frame, name="refresh_thread")
        self._refresh_thread.start()

        #Start the shot detection loop
        self._shot_detection_thread = Thread(target=self.detect_shots, name="shot_detection_thread")
        self._shot_detection_thread.start()
        if not self._shutdown:
            Tkinter.mainloop()
            self._window.destroy()

if __name__ == "__main__":
    # Start the main window
    mainWindow = MainWindow(Configurator())
    mainWindow.main()
