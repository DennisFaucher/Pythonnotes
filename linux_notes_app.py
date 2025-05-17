#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0') # Gdk is needed for key constants
from gi.repository import Gtk, Gio, GLib, Pango, PangoCairo

import os
import sys
import re
import markdown # For Markdown to HTML conversion for printing
import subprocess # For opening the PDF
from pathlib import Path # For robust file URI generation
from gi.repository import Gdk, GdkPixbuf # For Gdk.KEY_Return etc. and Pixbuf

from markdown.treeprocessors import Treeprocessor
from markdown.extensions import Extension

# --- WebKit2 for Rich Preview ---
WEBKIT_AVAILABLE = False
try:
    gi.require_version('WebKit2', '4.1') # Try 4.1 first
    from gi.repository import WebKit2
    WEBKIT_AVAILABLE = True
except (ValueError, ImportError):
    try: # Fallback to 4.0 if 4.1 not available
        gi.require_version('WebKit2', '4.0')
        from gi.repository import WebKit2
        WEBKIT_AVAILABLE = True
    except (ValueError, ImportError):
        print("WebKit2 not found. Image preview will be text-based. Install libwebkit2gtk-4.1-dev or similar.")

try:
    from weasyprint import HTML
except ImportError:
    HTML = None # WeasyPrint not installed

APP_ID = "com.vibecoding.gemini.linuxnotes"
NOTES_DIR_NAME = "notes"

# --- Markdown Extension for List Nesting Level ---
class ListNestingTreeprocessor(Treeprocessor):
    def run(self, root):
        # Process top-level lists directly under the root (e.g. document body)
        for element in root: # root usually doesn't have a tag, its children are blocks
            if element.tag in ("ul", "ol"):
                self._process_list_items_and_their_sublists(element, 0)
        return root # Must return the root

    def _process_list_items_and_their_sublists(self, list_element, item_level):
        # list_element is <ul> or <ol>
        # item_level is the nesting level for <li> items in THIS list_element
        for li_item in list_element:
            if li_item.tag == "li":
                li_item.set("data-li-level", str(item_level))
                # Now, check if this li_item itself contains sub-lists
                for sub_element_within_li in li_item:
                    if sub_element_within_li.tag in ("ul", "ol"):
                        # Items of this sub-list are at item_level + 1
                        self._process_list_items_and_their_sublists(sub_element_within_li, item_level + 1)

class ListNestingExtension(Extension):
    def extendMarkdown(self, md):
        md.treeprocessors.register(ListNestingTreeprocessor(md.parser), 'listnesting', 15) # Priority 15


# Determine the script's directory to locate the notes subdirectory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(SCRIPT_DIR, NOTES_DIR_NAME)

# TreeStore column constants
COL_DISPLAY_NAME = 0
COL_ICON = 1
COL_FULL_PATH = 2 # Relative path from NOTES_DIR
COL_ITEM_TYPE = 3 # 'folder' or 'note'
COL_IS_EDITABLE = 4 # For the display name column, to allow renaming (future use)

class MarkdownNotesWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.current_note_filename = None
        self.unsaved_changes = False
        self.preview_mode = False # Correctly initialized for preview mode

        self.set_default_size(800, 600)
        self.set_position(Gtk.WindowPosition.CENTER)

        self._setup_actions() # Setup actions first
        self._init_ui()
        self._populate_tree_model() # Changed from populate_note_list
        self.connect("delete-event", self.on_window_delete_event)

        # Apply custom CSS for the border
        style_provider = Gtk.CssProvider()
        css = """
        #note-list-pane {
            border-right-width: 1px;
            border-right-style: solid;
            border-right-color: #cccccc; /* A light grey color, adjust as needed */
        }
        """
        style_provider.load_from_data(css.encode()) # encode() is important for Python 3
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _init_ui(self):
        # HeaderBar
        self.header_bar = Gtk.HeaderBar()
        self.header_bar.set_show_close_button(True)

        # Custom title area with Stack for Title/Search
        self.title_stack = Gtk.Stack()
        self.title_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.title_label = Gtk.Label(label="Linux Notes") # Initial title
        self.title_stack.add_named(self.title_label, "title_label_page")

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.connect("search-changed", self.on_search_entry_changed)
        # stop-search is emitted when Escape is pressed or the clear icon is clicked
        self.search_entry.connect("stop-search", self.on_search_entry_stop_search)
        self.title_stack.add_named(self.search_entry, "search_entry_page")

        self.header_bar.set_custom_title(self.title_stack)
        self.title_stack.set_visible_child_name("title_label_page") # Start with title visible

        self.set_titlebar(self.header_bar)

        new_button = Gtk.Button.new_from_icon_name("document-new-symbolic", Gtk.IconSize.SMALL_TOOLBAR)
        new_button.connect("clicked", self.on_new_note_clicked)
        self.header_bar.pack_start(new_button)

        self.save_button = Gtk.Button.new_from_icon_name("document-save-symbolic", Gtk.IconSize.SMALL_TOOLBAR)
        self.save_button.set_action_name("win.save") # Link button to the "save" action
        # Sensitivity will be handled by the action's enabled state
        self.header_bar.pack_start(self.save_button)

        delete_button = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.SMALL_TOOLBAR)
        delete_button.connect("clicked", self.on_delete_note_clicked)
        self.header_bar.pack_start(delete_button)

        self.preview_toggle_button = Gtk.ToggleButton.new_with_label("👁️")
        self.preview_toggle_button.connect("toggled", self.on_preview_toggled)
        self.preview_toggle_button.set_sensitive(False) # Disabled initially until a note is open
        self.header_bar.pack_start(self.preview_toggle_button)

        search_button = Gtk.Button.new_with_label("🔍")
        search_button.connect("clicked", self.on_search_button_clicked)
        self.header_bar.pack_end(search_button) # Search icon on the right

        print_button = Gtk.Button.new_with_label("🖨️")
        print_button.connect("clicked", self.on_print_note_clicked)
        self.header_bar.pack_end(print_button) # Print icon to the very right

        # Main layout: Paned view
        main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(main_paned)

        # Left Pane: Note List
        left_scrolled_window = Gtk.ScrolledWindow()
        left_scrolled_window.set_hexpand(False)
        left_scrolled_window.set_vexpand(True)
        left_scrolled_window.set_min_content_width(200)
        left_scrolled_window.set_name("note-list-pane") # Assign a name for CSS targeting

        # TreeStore model: Display Name, Icon, Full Path, Item Type, Editable
        self.tree_model = Gtk.TreeStore(str, GdkPixbuf.Pixbuf, str, str, bool)

        self.tree_view = Gtk.TreeView(model=self.tree_model)
        self.tree_view.set_headers_visible(False) # No column headers
        self.tree_view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.tree_view.get_selection().connect("changed", self._on_tree_selection_changed)
        self.tree_view.connect("button-press-event", self._on_tree_view_button_press)

        # Renderer for Icon and Text
        renderer_pixbuf = Gtk.CellRendererPixbuf()
        renderer_text = Gtk.CellRendererText()
        # renderer_text.set_property("editable", True) # For future in-place renaming
        # renderer_text.connect("edited", self._on_tree_item_renamed_entry)

        column = Gtk.TreeViewColumn("Notes")
        column.pack_start(renderer_pixbuf, False)
        column.pack_start(renderer_text, True)
        column.add_attribute(renderer_pixbuf, "pixbuf", COL_ICON)
        column.add_attribute(renderer_text, "text", COL_DISPLAY_NAME)
        # column.add_attribute(renderer_text, "editable", COL_IS_EDITABLE) # For future
        self.tree_view.append_column(column)

        left_scrolled_window.add(self.tree_view)
        main_paned.pack1(left_scrolled_window, resize=False, shrink=False)

        # Right Pane: Stack for Editor and Preview
        self.view_stack = Gtk.Stack()
        self.view_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        self.editor_scrolled_window = Gtk.ScrolledWindow() # For the TextView
        self.editor_scrolled_window.set_hexpand(True)
        self.editor_scrolled_window.set_vexpand(True)

        self.editor_textview = Gtk.TextView()
        self.editor_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor_textview.set_monospace(True) # Monospace is good for Markdown
        self.editor_textview.set_sensitive(False) # Disabled initially
        self.editor_buffer = self.editor_textview.get_buffer()
        self.editor_textview.connect("key-press-event", self.on_editor_key_press)
        self.editor_buffer.connect("changed", self.on_buffer_changed)
        self.editor_scrolled_window.add(self.editor_textview)
        self.view_stack.add_named(self.editor_scrolled_window, "editor")

        if WEBKIT_AVAILABLE:
            self.preview_webview = WebKit2.WebView()
            # No need for a separate ScrolledWindow, WebView handles scrolling
            self.view_stack.add_named(self.preview_webview, "preview")
        else:
            # Fallback to Gtk.Label based preview if WebKit is not available
            self.preview_scrolled_window = Gtk.ScrolledWindow()
            self.preview_scrolled_window.set_hexpand(True)
            self.preview_scrolled_window.set_vexpand(True)
            self.preview_label = Gtk.Label(label="[Preview requires WebKit2Gtk]", xalign=0.0, yalign=0.0)
            self.preview_label.set_selectable(True)
            self.preview_label.set_line_wrap(True)
            self.preview_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self.preview_scrolled_window.add(self.preview_label)
            self.view_stack.add_named(self.preview_scrolled_window, "preview")

        main_paned.pack2(self.view_stack, resize=True, shrink=True)

        main_paned.set_position(250) # Initial position of the divider
        self.show_all()

    def _setup_actions(self):
        # Save Action
        self.save_action = Gio.SimpleAction.new("save", None)
        self.save_action.connect("activate", self._on_save_action_activate)
        self.add_action(self.save_action) # Add action to the window
        app = self.get_application()
        if app:
            app.set_accels_for_action("win.save", ["<Control>s"]) # Set accelerator on the application
        self.save_action.set_enabled(False) # Initially disabled

        # New Note Action
        new_note_action = Gio.SimpleAction.new("new_note", None)
        new_note_action.connect("activate", self.on_new_note_clicked) # Re-use existing handler
        self.add_action(new_note_action)
        if app:
            app.set_accels_for_action("win.new_note", ["<Control>n"])

    def _on_save_action_activate(self, action, param):
        # This is called when Ctrl+S is pressed or the save button is clicked
        self.save_current_note()

    def sanitize_filename(self, title):
        # Remove .mk if present, as we add it later
        if title.lower().endswith(".mk"):
            title = title[:-3] # Corrected for .mk extension
        # Replace spaces with underscores, remove special characters
        s = title.strip() # Preserve original case
        s = re.sub(r'\s+', '_', s)
        s = re.sub(r'[^\w.-]', '', s)
        return s + ".mk" if s else "untitled.mk"

    def get_title_from_filename(self, filename): # Removed typo 'ls'
        if filename.lower().endswith(".mk"): # Check with .lower() for robustness
            return filename[:-3].replace("_", " ") # Preserve original case, remove .title()
        return filename

    def _populate_tree_model(self, path_to_select=None):
        self.tree_model.clear()
        if not os.path.exists(NOTES_DIR):
            try:
                os.makedirs(NOTES_DIR)
            except OSError as e:
                self.show_message_dialog("Error", f"Could not create notes directory: {NOTES_DIR}\n{e}", "error")
                return
        
        self._load_directory_into_tree(None, NOTES_DIR, "") # Start with no parent_iter, base NOTES_DIR, empty relative path

        self.tree_view.expand_all() # Expand all folders initially

        if path_to_select:
            self._select_path_in_tree(path_to_select)

        # Re-apply filter if global search was active
        if hasattr(self, 'search_entry') and self.title_stack.get_visible_child_name() == "search_entry_page":
            current_search_text = self.search_entry.get_text()
            if current_search_text:
                self._filter_tree_view(current_search_text)

    def _load_directory_into_tree(self, parent_iter, directory_full_path, current_relative_path):
        icon_theme = Gtk.IconTheme.get_default()
        folder_icon = icon_theme.load_icon("folder-symbolic", 16, 0)
        note_icon = icon_theme.load_icon("text-x-generic-symbolic", 16, 0) # Or "accessories-text-editor-symbolic"
        
        try:
            items = sorted(os.listdir(directory_full_path), key=lambda s: s.lower())
        except OSError as e:
            self.show_message_dialog("Error", f"Could not read directory: {directory_full_path}\n{e}", "error")
            return

        for item_name in items:
            item_full_path = os.path.join(directory_full_path, item_name)
            item_relative_path = os.path.join(current_relative_path, item_name)

            if os.path.isdir(item_full_path):
                # It's a folder
                child_iter = self.tree_model.append(parent_iter, [item_name, folder_icon, item_relative_path, "folder", False])
                self._load_directory_into_tree(child_iter, item_full_path, item_relative_path)
            elif item_name.endswith(".mk"):
                # It's a note file
                display_title = self.get_title_from_filename(item_name)
                self.tree_model.append(parent_iter, [display_title, note_icon, item_relative_path, "note", False])

    def _on_tree_selection_changed(self, selection):
        model, tree_iter = selection.get_selected()
        if not tree_iter: # Nothing selected or selection cleared
            self.current_note_filename = None
            self.editor_buffer.set_text("")
            self.editor_textview.set_sensitive(False)
            self.preview_toggle_button.set_sensitive(False)
            if self.preview_mode: self.preview_toggle_button.set_active(False) # Switch back to edit mode
            self.set_unsaved_changes(False) # This will update title_label to "Linux Notes"
            # Disable "New Note" if a folder isn't selected, or adapt later
            return

        if self.handle_unsaved_changes() == Gtk.ResponseType.CANCEL:
            # User cancelled, re-select previous row if possible or deselect
            if self.current_note_filename:
                self._select_path_in_tree(self.current_note_filename)
            return

        selected_path = model.get_value(tree_iter, COL_FULL_PATH)
        item_type = model.get_value(tree_iter, COL_ITEM_TYPE)

        if item_type == "note":
            self.current_note_filename = selected_path # This is now a relative path
            self.load_note_content(self.current_note_filename)
            self.editor_textview.set_sensitive(not self.preview_mode)
            self.preview_toggle_button.set_sensitive(True)
            self.set_unsaved_changes(False)
            if self.preview_mode:
                self.update_markdown_preview()
        elif item_type == "folder":
            # Folder selected, clear editor, disable preview, etc.
            self.current_note_filename = None # Or store selected folder path
            self.editor_buffer.set_text(f"Folder selected: {selected_path}\n\n(Create notes or subfolders here in the future)", -1)
            self.editor_textview.set_sensitive(False)
            self.preview_toggle_button.set_sensitive(False)
            if self.preview_mode: self.preview_toggle_button.set_active(False)
            self.set_unsaved_changes(False)
            self.title_label.set_text(f"Folder: {model.get_value(tree_iter, COL_DISPLAY_NAME)}")

    def _select_path_in_tree(self, path_to_select):
        """Iterates through the tree model to find and select the item with the given full_path."""
        if not path_to_select: return

        def find_path_recursive(model, current_iter):
            while current_iter:
                item_path = model.get_value(current_iter, COL_FULL_PATH)
                if item_path == path_to_select:
                    path_gtk = model.get_path(current_iter)
                    self.tree_view.expand_to_path(path_gtk)
                    self.tree_view.get_selection().select_iter(current_iter)
                    # Ensure the selected row is visible
                    GLib.idle_add(self.tree_view.scroll_to_cell, path_gtk, None, True, 0.5, 0.0)
                    return True
                
                if model.iter_has_child(current_iter):
                    child_iter = model.iter_children(current_iter)
                    if find_path_recursive(model, child_iter):
                        return True
                current_iter = model.iter_next(current_iter)
            return False

        root_iter = self.tree_model.get_iter_first()
        find_path_recursive(self.tree_model, root_iter)

    def load_note_content(self, filename):
        filepath = os.path.join(NOTES_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                md_content = f.read()
            self.editor_buffer.set_text(md_content, -1)
            self.editor_textview.grab_focus()
            if self.preview_mode: # If preview is active, update it
                self.update_markdown_preview()
        except Exception as e:
            self.show_message_dialog("Error", f"Could not load note: {filename}\n{e}", "error")
            self.editor_buffer.set_text("")

    def save_current_note(self):
        if not self.current_note_filename:
            return False

        filepath = os.path.join(NOTES_DIR, self.current_note_filename)
        start_iter, end_iter = self.editor_buffer.get_bounds()
        md_content = self.editor_buffer.get_text(start_iter, end_iter, True)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)
            self.set_unsaved_changes(False)
            return True
        except Exception as e:
            self.show_message_dialog("Error", f"Could not save note: {self.current_note_filename}\n{e}", "error")
            return False

    # Modified to accept action and param for GAction activation
    def on_new_note_clicked(self, widget_or_action, param=None):
        if self.handle_unsaved_changes() == Gtk.ResponseType.CANCEL:
            return

        dialog = Gtk.Dialog(title="Create New Note", transient_for=self, flags=0)
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK)

        content_area = dialog.get_content_area()
        label = Gtk.Label(label="Enter title for the new note:")
        content_area.add(label)
        entry = Gtk.Entry()
        entry.set_activates_default(True) 
        content_area.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK) 

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            title = entry.get_text()
            if title:
                filename = self.sanitize_filename(title)
                filepath = os.path.join(NOTES_DIR, filename)
                
                # Determine target directory (selected folder or root)
                target_dir_relative_path = ""
                selection = self.tree_view.get_selection()
                model, tree_iter = selection.get_selected()
                if tree_iter:
                    item_type = model.get_value(tree_iter, COL_ITEM_TYPE)
                    item_path = model.get_value(tree_iter, COL_FULL_PATH)
                    if item_type == "folder":
                        target_dir_relative_path = item_path
                    elif item_type == "note": # If a note is selected, create in its parent folder
                        target_dir_relative_path = os.path.dirname(item_path)
                
                final_filepath = os.path.join(NOTES_DIR, target_dir_relative_path, filename)
                final_relative_path = os.path.join(target_dir_relative_path, filename)

                if os.path.exists(filepath):
                    self.show_message_dialog("Note Exists", f"A note named '{filename}' already exists.", "warning")
                else:
                    try:
                        os.makedirs(os.path.dirname(final_filepath), exist_ok=True) # Ensure target dir exists
                        with open(final_filepath, "w", encoding="utf-8") as f:
                            f.write(self.get_empty_markdown_document(title)) # Pass the user-entered title
                        self._populate_tree_model(path_to_select=final_relative_path) # Repopulate and select
                        # Selection change will handle enabling editor etc.
                    except Exception as e:
                        self.show_message_dialog("Error", f"Could not create note: {filename}\n{e}", "error")
            else:
                self.show_message_dialog("No Title", "Note title cannot be empty.", "warning")
        dialog.destroy()

    def on_delete_note_clicked(self, widget):
        if not self.current_note_filename:
            self.show_message_dialog("No Note Selected", "Please select a note to delete.", "warning")
            return

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Are you sure you want to delete '{self.get_title_from_filename(self.current_note_filename)}'?",
        )
        dialog.set_default_response(Gtk.ResponseType.NO)
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            filepath = os.path.join(NOTES_DIR, self.current_note_filename)
            try:
                os.remove(filepath)
                self.current_note_filename = None
                self.editor_buffer.set_text("")
                self.editor_textview.set_sensitive(False) 
                self.preview_toggle_button.set_sensitive(False) 
                if self.preview_mode: self.preview_toggle_button.set_active(False) 
                self.set_unsaved_changes(False)
                self._populate_tree_model()
            except Exception as e:
                self.show_message_dialog("Error", f"Could not delete note: {self.current_note_filename}\n{e}", "error")

    def on_print_note_clicked(self, widget):
        if not self.current_note_filename:
            self.show_message_dialog("No Note Selected", "Please select a note to print.", "warning")
            return

        if not self.editor_buffer.get_text(self.editor_buffer.get_start_iter(), self.editor_buffer.get_end_iter(), True).strip():
            self.show_message_dialog("Empty Note", "Cannot print an empty note.", "info")
            return


        if HTML is None:
            self.show_message_dialog("Error: WeasyPrint not found",
                                     "The WeasyPrint library is required to generate PDFs.\nPlease install it using: pip install WeasyPrint", "error")
            return

        # Get Markdown content
        start_iter, end_iter = self.editor_buffer.get_bounds()
        md_text = self.editor_buffer.get_text(start_iter, end_iter, True)


        # Convert Markdown to HTML
        # Removed 'sane_lists' to allow default Markdown behavior for lists,
        # which tends to wrap multi-paragraph list items in <p> tags.
        html_body_content = markdown.markdown(md_text, extensions=['fenced_code', 'tables', 'attr_list'])

        # Basic CSS for WeasyPrint to improve list rendering and general appearance
        css_styles = """
        <style>
            body { font-family: sans-serif; line-height: 1.6; margin: 20px; }
            ul, ol { padding-left: 2em; margin-top: 0.5em; margin-bottom: 0.5em; }
            li { 
                margin-bottom: 0.25em; 
                /* white-space: pre-wrap !important; */ /* Removed to allow normal flow; use <p> or <br> for explicit newlines */
            }
            li p { /* For paragraphs explicitly created within list items (by blank lines in markdown) */
                margin-top: 0; 
                margin-bottom: 0.25em; 
                white-space: pre-wrap !important; /* Also apply to paragraphs within list items */
                /* white-space: normal; /* Let paragraphs flow normally, overriding li's pre-wrap if needed */
            }
            p { margin-top: 0; margin-bottom: 1em; }
            h1, h2, h3, h4, h5, h6 { margin-top: 1.2em; margin-bottom: 0.6em; line-height: 1.2; }
            code, pre { font-family: monospace; }
            pre { 
                background-color: #f4f4f4; 
                padding: 1em; 
                overflow-x: auto; 
                border: 1px solid #ddd;
                border-radius: 4px;
                white-space: pre; /* Stricter whitespace for pre blocks */
            }
            img { max-width: 100%; height: auto; display: block; margin-bottom: 0.5em; }
            /* Handle widths/heights from img attributes */
            img[width$="%"] { width: attr(width); }
            img[width$="px"] { width: attr(width) !important; } /* Diagnostic: add !important */
            img[height$="%"] { height: attr(height); }
            img[height$="px"] { height: attr(height) !important; } /* Diagnostic: add !important */
        </style>
        """ # Added DOCTYPE
        html_full_content = f"<!DOCTYPE html><html><head>{css_styles}</head><body>{html_body_content}</body></html>"

        # Define PDF filename and path (same name as note, in the same directory)
        pdf_filename_base = os.path.splitext(self.current_note_filename)[0]
        pdf_filepath = os.path.join(NOTES_DIR, pdf_filename_base + ".pdf")
        
        # Determine the base URL for resolving relative paths (e.g., for images)
        # This should be the directory containing the current note.
        note_file_path = Path(NOTES_DIR) / self.current_note_filename
        # Ensure base_url is the directory containing the note, not the note file itself
        if self.current_note_filename and os.path.dirname(self.current_note_filename):
            # Note is in a subfolder
            base_url_for_pdf = (Path(NOTES_DIR) / os.path.dirname(self.current_note_filename)).resolve().as_uri() + '/'
        else: # Note is in the root NOTES_DIR
            base_url_for_pdf = Path(NOTES_DIR).resolve().as_uri() + '/'

        try:
            # Generate PDF
            HTML(string=html_full_content, base_url=base_url_for_pdf).write_pdf(pdf_filepath)
            self.show_message_dialog("PDF Generated", f"PDF saved as:\n{pdf_filepath}.\n\nClick OK to open.", "info")

            # Open the PDF
            if sys.platform == "win32":
                os.startfile(pdf_filepath)
            elif sys.platform == "darwin": # macOS
                subprocess.call(["open", pdf_filepath])
            else: # Linux and other Unix-like
                subprocess.call(["xdg-open", pdf_filepath])

        except Exception as e:
            self.show_message_dialog("PDF Generation Error", f"Could not generate or open PDF:\n{e}", "error")

    # The following methods are no longer needed for PDF generation
    # def _begin_print_cb(self, operation, context):
    #     operation.set_n_pages(1)

    # def _draw_page_cb(self, operation, context, page_nr):
    # ... (rest of the old _draw_page_cb method)

    def on_buffer_changed(self, buffer):
        if self.current_note_filename and not self.preview_mode: 
            self.set_unsaved_changes(True)
        
        if self.preview_mode and self.current_note_filename:
            GLib.idle_add(self.update_markdown_preview)

    def on_preview_toggled(self, button):
        self.preview_mode = button.get_active()
        if self.preview_mode:
            self.update_markdown_preview()
            self.view_stack.set_visible_child_name("preview")
            self.editor_textview.set_editable(False)
        else:
            self.view_stack.set_visible_child_name("editor")
            # Find bar visibility is handled by its own toggle, don't force hide here
            self.editor_textview.set_editable(True)
            if self.current_note_filename: 
                self.editor_textview.grab_focus()

    def update_markdown_preview(self):
        if not self.current_note_filename or not self.preview_mode: 
            return False 
        start_iter, end_iter = self.editor_buffer.get_bounds()
        md_text = self.editor_buffer.get_text(start_iter, end_iter, True)
        
        # Generate HTML body content using the same extensions as PDF for consistency
        html_body_content = markdown.markdown(md_text, extensions=['fenced_code', 'tables', 'attr_list', ListNestingExtension()])

        if WEBKIT_AVAILABLE:
            # Use the same CSS as for PDF generation for consistency
            # (Assuming css_styles is defined similarly to on_print_note_clicked or can be accessed)
            # For simplicity, let's redefine a basic one here or share it.
            # Ideally, self.get_pdf_css_styles() would be a helper.
            css_styles = self._get_embedded_css_for_html() # New helper method
            full_html_content = f"<html><head>{css_styles}</head><body>{html_body_content}</body></html>"

            if self.current_note_filename:
                note_file_path = Path(NOTES_DIR) / self.current_note_filename
                # Correct base_uri for notes in subdirectories
                if os.path.dirname(self.current_note_filename):
                    base_uri = (Path(NOTES_DIR) / os.path.dirname(self.current_note_filename)).resolve().as_uri() + '/'
                else:
                    base_uri = Path(NOTES_DIR).resolve().as_uri() + '/'
            else:
                base_uri = Path(NOTES_DIR).resolve().as_uri() + '/' # Fallback to notes dir
            self.preview_webview.load_html(full_html_content, base_uri)
        else:
            # Fallback to Pango markup for Gtk.Label
            pango_content = self._html_to_pango(html_body_content) # Pass body content
            if hasattr(self, 'preview_label'): # Check if fallback label exists
                self.preview_label.set_markup(pango_content)
            else: # Should not happen if _init_ui is correct
                print("Error: preview_label not found for fallback preview.")
        return False 


    def _html_to_pango(self, html_text):
        # Order of replacements can be important

        # Headings
        html_text = re.sub(r'<h1.*?>(.*?)</h1>', r'<span size="xx-large" weight="bold">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL)
        html_text = re.sub(r'<h2.*?>(.*?)</h2>', r'<span size="x-large" weight="bold">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL)
        html_text = re.sub(r'<h3.*?>(.*?)</h3>', r'<span size="large" weight="bold">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL)
        html_text = re.sub(r'<h4.*?>(.*?)</h4>', r'<span weight="bold">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL) # Pango 'medium' is default size
        html_text = re.sub(r'<h5.*?>(.*?)</h5>', r'<span weight="bold" size="small">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL)
        html_text = re.sub(r'<h6.*?>(.*?)</h6>', r'<span weight="bold" size="x-small">\1</span>\n', html_text, flags=re.IGNORECASE | re.DOTALL)

        # Bold and Italic
        # Ensure these run before general <b> and <i> if HTML could contain both
        html_text = re.sub(r'<strong>(.*?)</strong>', r'<b>\1</b>', html_text, flags=re.IGNORECASE | re.DOTALL)
        html_text = re.sub(r'<em>(.*?)</em>', r'<i>\1</i>', html_text, flags=re.IGNORECASE | re.DOTALL)
        # Handle plain <b> and <i> tags as well, in case markdown lib outputs them or for robustness
        html_text = re.sub(r'<b>(.*?)</b>', r'<b>\1</b>', html_text, flags=re.IGNORECASE | re.DOTALL) # Simplified, was <b.*?>
        html_text = re.sub(r'<i>(.*?)</i>', r'<i>\1</i>', html_text, flags=re.IGNORECASE | re.DOTALL) # Simplified, was <i.*?>

        # Preformatted text
        def pre_block_to_pango(match):
            content = match.group(1)
            # Escape content that's inside <pre> so Pango displays it literally
            # Do not strip content here, to preserve leading/trailing spaces within the code block
            escaped_content = GLib.markup_escape_text(content)
            return f'<tt>\n{escaped_content}\n</tt>\n'
        # Process <pre><code> blocks first as they are more specific
        html_text = re.sub(r'<pre><code.*?>(.*?)</code></pre>', pre_block_to_pango, html_text, flags=re.IGNORECASE | re.DOTALL)
        # Then general <pre> blocks (if any, without <code>)
        html_text = re.sub(r'<pre.*?>(.*?)</pre>', pre_block_to_pango, html_text, flags=re.IGNORECASE | re.DOTALL)
        # Then inline <code>. This should be applied after <pre><code> blocks are handled.
        html_text = re.sub(r'<code>(.*?)</code>', r'<tt>\1</tt>', html_text, flags=re.IGNORECASE | re.DOTALL)

        # Paragraphs - Pango uses newlines.
        html_text = re.sub(r'<p.*?>(.*?)</p>', r'\1\n\n', html_text, flags=re.IGNORECASE | re.DOTALL) # Add double newline for paragraph spacing

        # Line breaks
        html_text = re.sub(r'<br\s*/?>', '\n', html_text, flags=re.IGNORECASE)

        # Images (convert to textual placeholder for Pango preview)
        def img_to_pango_placeholder(match):
            full_tag_match = match.group(0)
            src_attr_match = re.search(r'src="([^"]*)"', full_tag_match, re.IGNORECASE)
            alt_attr_match = re.search(r'alt="([^"]*)"', full_tag_match, re.IGNORECASE)

            src = src_attr_match.group(1) if src_attr_match else "unknown"
            alt = alt_attr_match.group(1) if alt_attr_match else "" # alt can be empty

            alt_display = f"<tt>{GLib.markup_escape_text(alt)}</tt>" if alt else "(No alt text)"
            src_display = f"<tt>{GLib.markup_escape_text(src)}</tt>"
            return f"\n<i>[Image: {alt_display} (src: {src_display})]</i>\n"

        # Make the image regex more robust: match <img ... > potentially followed by </img ... >
        # Use re.S (re.DOTALL) to ensure . matches newlines within the tag attributes if any.
        html_text = re.sub(r'<img[^>]*>(?:</img>)?', img_to_pango_placeholder, html_text, flags=re.IGNORECASE | re.S)

        # LISTS - Processed using data-li-level attribute
        # This section should run after inline formatting (bold, italic, etc.)
        # has been applied to the content within <li> tags, but before
        # generic stripping of unknown tags.    

        max_styled_depth = 3  # Max depth for distinct styling (0, 1, 2...)
        bullets = ['•', '-', '•', '-'] # Bullets for different depths
        indent_char = "       "    # Two space per indent level

        # Process <li> tags from deepest to shallowest to avoid issues with nested replacements
        for depth in range(max_styled_depth, -1, -1):
            current_indent = indent_char * depth
            current_bullet = bullets[depth] if depth < len(bullets) else bullets[-1]
            
            # Regex to find <li> tags with the specific data-li-level
            # The content (.*?) is captured. We assume it's already Pango-fied for inline styles.
            pattern = r'<li data-li-level="' + str(depth) + r'">(.*?)</li>'
            
            # Using a lambda for replacement to correctly format the captured group
            # m.group(1).strip() helps remove surrounding newlines from HTML that might mess up Pango layout
            replacement_func = lambda m: f'\n{current_indent}{current_bullet} {m.group(1).strip()}\n'
            
            html_text = re.sub(pattern, replacement_func, html_text, flags=re.S | re.I)

        # After processing all <li> based on depth, strip the <ul> and <ol> container tags.
        html_text = re.sub(r'</?(ul|ol).*?>', '', html_text, flags=re.I | re.S)
        # Strip any remaining <li> tags that might not have matched (e.g., no data-li-level or too deep)
        # This might be too aggressive if some <li> are legitimate but unstyled. For now, it cleans up.
        html_text = re.sub(r'</?li.*?>', '', html_text, flags=re.I | re.S)

        # Horizontal rule
        html_text = re.sub(r'<hr\s*/?>', '------------------------------\n', html_text, flags=re.IGNORECASE)

        # REMOVED: The following line was stripping our Pango tags:
        # html_text = re.sub(r'<[^>]+>', '', html_text)

        return html_text.strip()

    def _get_embedded_css_for_html(self):
        # Helper to provide consistent CSS for both PDF and WebView preview
        return """
        <style>
            body { font-family: sans-serif; line-height: 1.6; margin: 20px; }
            ul, ol { padding-left: 2em; margin-top: 0.5em; margin-bottom: 0.5em; }
            li { 
                margin-bottom: 0.25em; 
                /* white-space: pre-wrap !important; */ /* Removed to allow normal flow; use <p> or <br> for explicit newlines */
            }
            li p { /* For paragraphs explicitly created within list items (by blank lines in markdown) */
                margin-top: 0; 
                margin-bottom: 0.25em; 
                white-space: pre-wrap !important; /* Also apply to paragraphs within list items */
            }
            p { margin-top: 0; margin-bottom: 1em; }
            h1, h2, h3, h4, h5, h6 { margin-top: 1.2em; margin-bottom: 0.6em; line-height: 1.2; }
            code, pre { font-family: monospace; }
            pre { 
                background-color: #f4f4f4; padding: 1em; overflow-x: auto; 
                border: 1px solid #ddd; border-radius: 4px; white-space: pre;
            }
            img { max-width: 100%; height: auto; display: block; margin-bottom: 0.5em; }
            img[width$="%"] { width: attr(width); } /* Added for consistency */
            img[width$="px"] { width: attr(width) !important; } /* Added for pixel widths, diagnostic !important */
            img[height$="%"] { height: attr(height); } /* Added for consistency */
            img[height$="px"] { height: attr(height) !important; } /* Added for pixel heights, diagnostic !important */
        </style>
        """

    def _on_tree_view_button_press(self, treeview, event):
        if event.button == 3: 
            # Get the path at the click position
            pthinfo = treeview.get_path_at_pos(int(event.x), int(event.y))
            if pthinfo is not None:
                path, col, cellx, celly = pthinfo
                treeview.grab_focus() # Focus the treeview
                treeview.set_cursor(path, col, False) # Select the row
                model = treeview.get_model()
                tree_iter = model.get_iter(path)
                item_type = model.get_value(tree_iter, COL_ITEM_TYPE)
                item_path = model.get_value(tree_iter, COL_FULL_PATH) # Relative path
                self._show_item_context_menu(event, item_type, item_path, tree_iter)
                return True 
        return False 

    def _show_item_context_menu(self, event, item_type, item_path, tree_iter):
        menu = Gtk.Menu()
        
        if item_type == "note":
            rename_item = Gtk.MenuItem(label="Rename Note")
            rename_item.connect("activate", self._on_rename_item_requested, tree_iter) # Pass iter
            menu.append(rename_item)
        elif item_type == "folder":
            # TODO: Add "New Note in Folder", "New Subfolder", "Rename Folder", "Delete Folder"
            pass # Placeholder for folder actions
        
        menu.show_all()
        menu.popup_at_pointer(event)

    def _on_rename_item_requested(self, menu_item, tree_iter): # Changed to accept tree_iter
        model = self.tree_model # Use self.tree_model
        old_relative_path = model.get_value(tree_iter, COL_FULL_PATH)
        old_filename = os.path.basename(old_relative_path) # Get just the filename part
        old_display_title = self.get_title_from_filename(old_filename)

        dialog = Gtk.Dialog(title="Rename Note", transient_for=self, flags=0)
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK)

        content_area_box = dialog.get_content_area() 
        content_area_box.set_border_width(10) 
        content_area_box.set_spacing(6)

        entry_label = Gtk.Label(label="Enter new title for the note:", xalign=0)
        content_area_box.pack_start(entry_label, False, False, 0)
        
        entry = Gtk.Entry()
        entry.set_text(old_display_title)
        entry.set_activates_default(True)
        entry.grab_focus()
        content_area_box.pack_start(entry, True, True, 0)
        
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            new_display_title = entry.get_text().strip()
            if not new_display_title:
                self.show_message_dialog("Error", "New title cannot be empty.", "error")
                dialog.destroy()
                return

            new_basename_candidate = self.sanitize_filename(new_display_title)
            
            if new_basename_candidate == old_filename: # If only case changed or no change
                dialog.destroy()
                # TODO: Handle case-only renames if filesystem is case-sensitive
                return

            # old_filepath = os.path.join(NOTES_DIR, old_filename) # Not needed with relative paths
            # new_filepath_candidate = os.path.join(NOTES_DIR, new_basename_candidate) # Corrected variable name
            # Construct new full relative path
            parent_dir_relative = os.path.dirname(old_relative_path)
            new_relative_path_candidate = os.path.join(parent_dir_relative, new_basename_candidate)
            new_absolute_path_candidate = os.path.join(NOTES_DIR, new_relative_path_candidate)

            if os.path.exists(new_absolute_path_candidate):
                self.show_message_dialog("Error", f"A file or folder named '{new_basename_candidate}' already exists in this location.", "error")
                dialog.destroy()
                return

            is_renaming_current_note = (old_relative_path == self.current_note_filename)

            if is_renaming_current_note and self.unsaved_changes:
                if not self.save_current_note(): 
                    self.show_message_dialog("Save Failed", "Could not save current note before renaming. Rename aborted.", "error")
                    dialog.destroy()
                    return
            
            try:
                os.rename(os.path.join(NOTES_DIR, old_relative_path), new_absolute_path_candidate)
            except OSError as e:
                self.show_message_dialog("Error", f"Could not rename note: {e}", "error")
                dialog.destroy()
                return

            if is_renaming_current_note:
                self.current_note_filename = new_relative_path_candidate # Update to new relative path
                self.set_unsaved_changes(False) 
            
            # Determine which path to reselect. If the renamed note was the current one, select its new path.
            # Otherwise, keep the currently selected note (if any) selected.
            path_to_reselect_after_rename = new_relative_path_candidate \
                if is_renaming_current_note else (self.current_note_filename if self.current_note_filename != old_relative_path else None)

            self._populate_tree_model(path_to_select=path_to_reselect_after_rename)

        dialog.destroy()


    def on_format_button_clicked(self, widget, style): # This method is no longer called by UI
        buffer = self.editor_buffer
        bounds = buffer.get_selection_bounds()
        buffer.begin_user_action()
        if style in ["bold", "italic"]:
            if not bounds:
                self.show_message_dialog("Formatting", "Please select text to format bold/italic.", "info")
                buffer.end_user_action()
                return
            start, end = bounds
            text = buffer.get_text(start, end, False)
            md_chars = "**" if style == "bold" else "*"
            if text.startswith(md_chars) and text.endswith(md_chars) and len(text) >= 2 * len(md_chars):
                new_text = text[len(md_chars):-len(md_chars)]
            else:
                new_text = f"{md_chars}{text}{md_chars}"
            buffer.delete(start, end)
            buffer.insert(start, new_text)
        elif style in ["h1", "h2", "h3"]:
            prefixes = {"h1": "# ", "h2": "## ", "h3": "### "}
            target_prefix = prefixes[style]
            all_heading_prefixes = list(prefixes.values())
            self._toggle_line_prefix_for_selection(target_prefix, all_heading_prefixes)
        buffer.end_user_action()
        if self.current_note_filename:
            self.set_unsaved_changes(True)

    def _toggle_line_prefix_for_selection(self, target_prefix, all_prefixes_to_check, is_list=False, list_item_char=None): # No longer called by UI
        buffer = self.editor_buffer
        bounds = buffer.get_selection_bounds()
        if bounds:
            start_iter, end_iter = bounds
            start_line = start_iter.get_line()
            end_line = end_iter.get_line()
            if end_iter.get_line_offset() == 0 and end_iter.get_line() > start_iter.get_line():
                end_line -=1
        else: 
            cursor_iter = buffer.get_iter_at_mark(buffer.get_insert())
            start_line = end_line = cursor_iter.get_line()
        numbered_list_item_count = 1
        for line_num in range(start_line, end_line + 1):
            line_start_iter = buffer.get_iter_at_line(line_num)
            line_end_iter = line_start_iter.copy()
            if not line_end_iter.ends_line():
                line_end_iter.forward_to_line_end()
            line_text = buffer.get_text(line_start_iter, line_end_iter, False)
            current_prefix_is_target = False
            if is_list and list_item_char == '1': 
                if re.match(r"^\d+\.\s", line_text):
                    current_prefix_is_target = True
            elif line_text.startswith(target_prefix):
                 current_prefix_is_target = True
            for p_check in sorted(all_prefixes_to_check, key=len, reverse=True): 
                if (is_list and list_item_char == '1' and re.match(r"^\d+\.\s", line_text)) or \
                   (not (is_list and list_item_char == '1') and line_text.startswith(p_check)):
                    actual_prefix_len = 0
                    if is_list and list_item_char == '1':
                        match = re.match(r"^(\d+\.\s)", line_text)
                        if match: actual_prefix_len = len(match.group(1))
                    else:
                        actual_prefix_len = len(p_check)
                    if actual_prefix_len > 0:
                        del_iter_end = buffer.get_iter_at_line_offset(line_num, actual_prefix_len)
                        buffer.delete(buffer.get_iter_at_line(line_num), del_iter_end)
                    break 
            if not current_prefix_is_target:
                insert_iter = buffer.get_iter_at_line(line_num)
                if is_list and list_item_char == '1':
                    buffer.insert(insert_iter, f"{numbered_list_item_count}. ")
                    numbered_list_item_count += 1
                else:
                    buffer.insert(insert_iter, target_prefix)

    def on_list_button_clicked(self, widget, list_type): # This method is no longer called by UI
        buffer = self.editor_buffer
        buffer.begin_user_action()
        if list_type == "bullet":
            self._toggle_line_prefix_for_selection("- ", ["- ", "* ", "+ ", r"^\d+\.\s"], is_list=True, list_item_char='-')
        elif list_type == "numbered":
            self._toggle_line_prefix_for_selection("1. ", ["- ", "* ", "+ ", r"^\d+\.\s"], is_list=True, list_item_char='1')
        buffer.end_user_action()
        if self.current_note_filename:
            self.set_unsaved_changes(True)

    def get_empty_markdown_document(self, title="New Note"):
        # Sanitize the title slightly for display as a heading (e.g., remove leading/trailing spaces)
        # The main sanitization for filename is done elsewhere.
        display_title = title.strip() if title else "New Note"
        return f"# {display_title}\n"

    def on_search_button_clicked(self, widget):
        if self.title_stack.get_visible_child_name() == "title_label_page":
            self.title_stack.set_visible_child_name("search_entry_page")
            self.search_entry.grab_focus()
        else: 
            self.search_entry.set_text("") 
            self.title_stack.set_visible_child_name("title_label_page")

    def on_search_entry_changed(self, entry):
        search_text = entry.get_text().lower().strip()
        self._filter_tree_view(search_text)

    def on_search_entry_stop_search(self, entry):
        self.title_stack.set_visible_child_name("title_label_page")
        self._filter_tree_view("") # Clear filter

    def _filter_tree_view(self, search_text):
        search_text = search_text.lower().strip()

        # This is a simple name-based filter. Content search is more complex with a tree.
        # Gtk.TreeModelFilter might be better for performance on large trees.
        def filter_row_recursive(model, tree_iter, visible):
            # Check current item
            display_name = model.get_value(tree_iter, COL_DISPLAY_NAME).lower()
            item_type = model.get_value(tree_iter, COL_ITEM_TYPE)
            
            # For now, only filter notes by name. Folders always visible if they contain visible notes.
            # A more sophisticated filter would hide empty folders.
            current_item_matches = search_text in display_name if item_type == "note" else True
            
            # If this item matches or search is empty, it's potentially visible
            # If it's a folder, its visibility also depends on its children
            # For simplicity, we'll just make notes visible/invisible based on name match
            # and keep folders always visible (or filter them too if desired)
            
            # This simple filter just makes the row visible/invisible based on its own name
            # It doesn't handle parent visibility based on child visibility well.
            # For a basic filter:
            return current_item_matches # This will be used by a TreeModelFilter

        # For now, we'll just re-populate and re-expand. A real filter is better.
        # This is a placeholder for a more robust filtering mechanism.
        # A proper filter would use Gtk.TreeModelFilter.
        # For now, if search_text is present, we might just show all and let user find.
        # Or, we can iterate and manually hide/show rows (less efficient).
        # The current _populate_tree_model already re-applies the filter if search is active.
        # The actual filtering logic within _populate_tree_model needs to be smarter or use TreeModelFilter.
        # For this iteration, the search will be very basic: it will re-trigger populate which
        # will then call _filter_tree_view if search text is present.
        # The _filter_tree_view itself is not fully implemented here for recursive visibility.
        pass # Placeholder: Proper tree filtering is complex and needs Gtk.TreeModelFilter

    def set_unsaved_changes(self, new_state):
        if self.unsaved_changes == new_state: 
            return

        self.unsaved_changes = new_state 
        if hasattr(self, 'save_action'): # Ensure save_action is initialized
            self.save_action.set_enabled(self.unsaved_changes and bool(self.current_note_filename))
        
        current_title_text = "Linux Notes" 
        if self.current_note_filename:
            current_title_text = self.get_title_from_filename(self.current_note_filename)
        if self.unsaved_changes: 
            self.title_label.set_text(current_title_text + "*")
        else:
            self.title_label.set_text(current_title_text)

    def handle_unsaved_changes(self):
        if not self.unsaved_changes:
            return Gtk.ResponseType.NO 

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            text="You have unsaved changes.",
            secondary_text="Do you want to save them before proceeding?"
        )
        dialog.add_button("Don't Save", Gtk.ResponseType.NO)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            if self.save_current_note():
                return Gtk.ResponseType.YES 
            else:
                return Gtk.ResponseType.CANCEL 
        elif response == Gtk.ResponseType.NO:
            return Gtk.ResponseType.NO 
        else: 
            return Gtk.ResponseType.CANCEL

    def on_window_delete_event(self, window, event):
        if self.unsaved_changes:
            response = self.handle_unsaved_changes()
            if response == Gtk.ResponseType.CANCEL:
                return True 
        return False 

    def on_editor_key_press(self, widget, event):
        """Handles key presses in the editor, specifically for auto-bulleting."""
        if event.keyval == Gdk.KEY_Return or event.keyval == Gdk.KEY_KP_Enter:
            buffer = self.editor_buffer
            insert_mark = buffer.get_insert()
            cursor_iter = buffer.get_iter_at_mark(insert_mark)
            
            current_line_number = cursor_iter.get_line()
            line_start_iter = buffer.get_iter_at_line(current_line_number)
            
            # Get text of the current line up to the cursor
            # If cursor is at start of line, this will be empty.
            # We need the text of the line *before* Enter creates a new one.
            # So, we check the line the cursor is currently on.
            line_end_iter = line_start_iter.copy()
            if not line_end_iter.ends_line():
                line_end_iter.forward_to_line_end()
            
            current_line_text = buffer.get_text(line_start_iter, line_end_iter, False)

            # Regex to find bullet prefixes like "  - item" or "  * item"
            # Group 1: The full prefix (e.g., "  - ")
            # Group 2: The actual bullet character (e.g., "-")
            # Group 3: The content after the bullet prefix
            bullet_pattern = r"^(\s*([-*+])\s+)(.*)"
            match = re.match(bullet_pattern, current_line_text)

            if match:
                # Schedule the action after GTK processes the Enter key
                # Pass the prefix (e.g., "  - ") and the content part
                full_prefix = match.group(1)
                content_after_prefix = match.group(3)
                GLib.idle_add(self._handle_enter_after_bullet_line, 
                              full_prefix, content_after_prefix, current_line_number)
        return False # Allow default processing of the key press

    def _handle_enter_after_bullet_line(self, prev_line_full_prefix, prev_line_content, prev_line_number):
        """Called after Enter on a bulleted line to insert new bullet or clear previous."""
        buffer = self.editor_buffer
        buffer.begin_user_action() # Group changes
        insert_mark = buffer.get_insert()
        current_iter = buffer.get_iter_at_mark(insert_mark)
        current_line_number = current_iter.get_line()

        # Ensure we are on the line immediately following the one where Enter was pressed
        if current_line_number == prev_line_number + 1:
            if prev_line_content.strip() == "": # Previous bullet item was empty (e.g. "  - ")
                # Delete the bullet prefix from the previous line
                prev_line_start_iter = buffer.get_iter_at_line(prev_line_number)
                prev_line_prefix_end_iter = buffer.get_iter_at_line_offset(prev_line_number, len(prev_line_full_prefix))
                buffer.delete(prev_line_start_iter, prev_line_prefix_end_iter)
            else: # Previous bullet item had content, so continue the list
                # Insert "- " at the current cursor position (which is on the new line)
                # GTK's default Enter behavior should handle indentation.
                buffer.insert(current_iter, "- ")
        buffer.end_user_action() # End group changes
        return GLib.SOURCE_REMOVE # Or False, to run only once

    def show_message_dialog(self, title, message, type="info"):
        if type == "error":
            msg_type = Gtk.MessageType.ERROR
        elif type == "warning":
            msg_type = Gtk.MessageType.WARNING
        else:
            msg_type = Gtk.MessageType.INFO

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=msg_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

class LinuxNotesApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MarkdownNotesWindow(application=self)
        self.window.present()

    def do_startup(self):
        Gtk.Application.do_startup(self)

        # Set a human-readable application name, which might help with system integration
        GLib.set_application_name("Linux Notes")
        if not os.path.exists(NOTES_DIR):
            try:
                os.makedirs(NOTES_DIR)
                print(f"Created notes directory at: {NOTES_DIR}")
            except OSError as e:
                print(f"Critical Error: Could not create notes directory: {NOTES_DIR}\n{e}", file=sys.stderr)

def main():
    app = LinuxNotesApp()
    exit_status = app.run(sys.argv)
    sys.exit(exit_status)

if __name__ == "__main__":
    main()