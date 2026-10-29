-- Catppuccin Mocha for AwesomeWM 4.3 (extends the stock default theme)
local gears     = require("gears")
local gfs       = require("gears.filesystem")
local theme_assets = require("beautiful.theme_assets")
local dpi       = require("beautiful.xresources").apply_dpi

local M = {
  base="#1e1e2e", mantle="#181825", crust="#11111b",
  surface0="#313244", surface1="#45475a", surface2="#585b70",
  overlay0="#6c7086", text="#cdd6f4", subtext="#a6adc8",
  blue="#89b4fa", mauve="#cba6f7", pink="#f5c2e7", red="#f38ba8",
  green="#a6e3a1", yellow="#f9e2af", peach="#fab387", teal="#94e2d5",
}

local theme = dofile(gfs.get_themes_dir() .. "default/theme.lua")

theme.font          = "JetBrains Mono Bold 10"
theme.wallpaper     = "@WALLPAPER@"

theme.bg_normal     = M.base
theme.bg_focus      = M.surface0
theme.bg_urgent     = M.red
theme.bg_minimize   = M.mantle
theme.bg_systray    = M.base

theme.fg_normal     = M.subtext
theme.fg_focus      = M.text
theme.fg_urgent     = M.crust
theme.fg_minimize   = M.overlay0

theme.useless_gap   = dpi(6)
theme.border_width  = dpi(2)
theme.border_normal = M.surface0
theme.border_focus  = M.blue
theme.border_marked = M.mauve

theme.taglist_bg_focus    = M.blue
theme.taglist_fg_focus    = M.crust
theme.taglist_bg_urgent   = M.red
theme.taglist_fg_urgent   = M.crust
theme.taglist_bg_occupied = M.surface0
theme.taglist_fg_occupied = M.text
theme.taglist_bg_empty    = M.base .. "00"
theme.taglist_fg_empty    = M.overlay0
theme.taglist_spacing     = dpi(4)
theme.taglist_shape       = gears.shape.rounded_rect
theme.taglist_shape_border_width = 0

theme.tasklist_bg_normal  = M.base .. "00"
theme.tasklist_bg_focus   = M.surface0
theme.tasklist_fg_focus   = M.text
theme.tasklist_shape      = gears.shape.rounded_rect
theme.tasklist_spacing    = dpi(4)
theme.tasklist_plain_task_name = true
theme.tasklist_disable_icon = false

theme.notification_bg      = M.mantle
theme.notification_fg      = M.text
theme.notification_border_color = M.blue
theme.notification_border_width = dpi(2)
theme.notification_shape   = function(cr, w, h) gears.shape.rounded_rect(cr, w, h, dpi(12)) end
theme.notification_margin  = dpi(14)
theme.notification_icon_size = dpi(48)

theme.hotkeys_bg           = M.mantle
theme.hotkeys_fg           = M.text
theme.hotkeys_border_color = M.blue
theme.hotkeys_modifiers_fg = M.mauve
theme.hotkeys_label_bg     = M.blue
theme.hotkeys_label_fg     = M.crust
theme.hotkeys_font         = "JetBrains Mono Bold 10"
theme.hotkeys_description_font = "JetBrains Mono 9"

theme.menu_height = dpi(26)
theme.menu_width  = dpi(170)
theme.menu_bg_normal = M.mantle
theme.menu_bg_focus  = M.blue
theme.menu_fg_focus  = M.crust
theme.menu_border_color = M.surface0
theme.menu_border_width = dpi(2)

theme.titlebar_bg_focus  = M.surface0
theme.titlebar_bg_normal = M.mantle
theme.titlebar_fg_focus  = M.text

-- regenerate the themed assets in our palette
theme.awesome_icon = theme_assets.awesome_icon(dpi(20), M.blue, M.crust)
theme.taglist_squares_sel   = theme_assets.taglist_squares_sel(dpi(4), M.blue)
theme.taglist_squares_unsel = theme_assets.taglist_squares_unsel(dpi(4), M.overlay0)
theme.icon_theme = "Papirus-Dark"

theme.palette = M
return theme
