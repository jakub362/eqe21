-- ─────────────────────────────────────────────────────────────
--  AwesomeWM 4.3 — Catppuccin Mocha (generated, safe to edit)
-- ─────────────────────────────────────────────────────────────
pcall(require, "luarocks.loader")

local gears     = require("gears")
local awful     = require("awful")
                  require("awful.autofocus")
local wibox     = require("wibox")
local beautiful = require("beautiful")
local naughty   = require("naughty")
local menubar   = require("menubar")
local hotkeys_popup = require("awful.hotkeys_popup")
local dpi       = require("beautiful.xresources").apply_dpi
require("awful.hotkeys_popup.keys")

-- ── GLib 2.86 compatibility ──────────────────────────────────
-- Fedora 44 ships GLib 2.86, where the Unix-specific Gio classes moved out
-- of the Gio namespace: Gio.UnixInputStream is now GioUnix.InputStream.
-- awful.spawn.with_line_callback still asks for the old name and dies with
-- "attempt to index a nil value (field 'UnixInputStream')", which takes
-- awful.widget.watch and easy_async* down with it.  Put the old names back
-- if lgi lets us, and keep a blocking fallback for when it does not.
local lgi = require("lgi")
local async_ok = pcall(function()
    if not lgi.Gio.UnixInputStream and lgi.GioUnix then
        lgi.Gio.UnixInputStream  = lgi.GioUnix.InputStream
        lgi.Gio.UnixOutputStream = lgi.GioUnix.OutputStream
    end
end)
async_ok = async_ok and lgi.Gio.UnixInputStream ~= nil

-- watch(cmd, seconds, cb, widget): awful.widget.watch when async works,
-- a timer + io.popen when it does not.  These commands are cheap.
local function watch(cmd, seconds, callback, widget)
    if async_ok then return awful.widget.watch(cmd, seconds, callback, widget) end
    local function tick()
        local f = io.popen(cmd)
        if not f then return end
        local out = f:read("*a") or ""
        f:close()
        callback(widget, out)
    end
    tick()
    gears.timer({ timeout = seconds, autostart = true, callback = tick })
    return widget
end

-- one-shot version of the same thing
local function read_async(cmd, callback)
    if async_ok then return awful.spawn.easy_async_with_shell(cmd, callback) end
    local f = io.popen(cmd)
    if not f then return end
    local out = f:read("*a") or ""
    f:close()
    callback(out)
end

-- ── error handling ───────────────────────────────────────────
if awesome.startup_errors then
    naughty.notify({ preset = naughty.config.presets.critical,
                     title = "Error during startup", text = awesome.startup_errors })
end
do
    local in_error = false
    awesome.connect_signal("debug::error", function (err)
        if in_error then return end
        in_error = true
        naughty.notify({ preset = naughty.config.presets.critical,
                         title = "An error happened", text = tostring(err) })
        in_error = false
    end)
end

-- ── theme / defaults ─────────────────────────────────────────
beautiful.init(gears.filesystem.get_configuration_dir() .. "theme.lua")
local C = beautiful.palette

terminal   = "kitty"
editor     = os.getenv("EDITOR") or "nano"
editor_cmd = terminal .. " -e " .. editor
modkey     = "Mod4"
local launcher = "rofi -show drun"

awful.layout.layouts = {
    awful.layout.suit.tile,
    awful.layout.suit.tile.bottom,
    awful.layout.suit.fair,
    awful.layout.suit.max,
    awful.layout.suit.floating,
}

naughty.config.defaults.timeout = 6
naughty.config.defaults.position = "top_right"

-- ── autostart ────────────────────────────────────────────────
local function run_once(cmd, pname)
    awful.spawn.with_shell(string.format("pgrep -x %s >/dev/null || (%s)", pname or cmd, cmd))
end
run_once("picom -b --config " .. os.getenv("HOME") .. "/.config/picom/picom.conf", "picom")
run_once("nm-applet --indicator", "nm-applet")
run_once("/usr/libexec/polkit-kde-authentication-agent-1", "polkit-kde-aut")
awful.spawn.with_shell("xsetroot -cursor_name left_ptr 2>/dev/null; true")

-- ── menu ─────────────────────────────────────────────────────
local myawesomemenu = {
   { "hotkeys",     function() hotkeys_popup.show_help(nil, awful.screen.focused()) end },
   { "manual",      terminal .. " -e man awesome" },
   { "edit config", editor_cmd .. " " .. awesome.conffile },
   { "restart",     awesome.restart },
   { "quit",        function() awesome.quit() end },
}
mymainmenu = awful.menu({ items = {
    { "awesome", myawesomemenu, beautiful.awesome_icon },
    { "terminal", terminal },
    { "files", "de-files" },
    { "power", "de-powermenu" },
}})
menubar.utils.terminal = terminal

-- ── widgets ──────────────────────────────────────────────────
local function pill(w, fg, bgalpha)
    return wibox.widget {
        {
            { w, left = dpi(12), right = dpi(12), widget = wibox.container.margin },
            fg = fg or C.text,
            widget = wibox.container.background,
        },
        bg = (bgalpha or C.base .. "CC"),
        shape = function(cr, wd, h) gears.shape.rounded_rect(cr, wd, h, dpi(12)) end,
        widget = wibox.container.background,
    }
end

local clock = wibox.widget.textclock('<span font="JetBrains Mono Bold 10">  %H:%M</span>', 20)
local cal = awful.widget.calendar_popup.month({
    style_month = { border_width = dpi(2), border_color = C.blue, bg_color = C.mantle, padding = dpi(8),
                    shape = function(cr,w,h) gears.shape.rounded_rect(cr,w,h,dpi(12)) end },
    style_header = { bg_color = C.blue, fg_color = C.crust, border_width = 0 },
    style_focus  = { bg_color = C.mauve, fg_color = C.crust, border_width = 0 },
    style_normal = { bg_color = C.mantle, fg_color = C.text, border_width = 0 },
    style_weekday= { bg_color = C.mantle, fg_color = C.blue, border_width = 0 },
})
cal:attach(clock, "tr")

local cpu = wibox.widget.textbox()
watch([[sh -c "grep -w cpu /proc/stat"]], 3, function(w, out)
    local u, n, s, i = out:match("(%d+) (%d+) (%d+) (%d+)")
    u, n, s, i = tonumber(u), tonumber(n), tonumber(s), tonumber(i)
    local total, busy = u + n + s + i, u + n + s
    if cpu.__t then
        local dt, db = total - cpu.__t, busy - cpu.__b
        if dt > 0 then w:set_markup(string.format('<span font="JetBrains Mono Bold 10">  %d%%</span>', math.floor(db / dt * 100 + 0.5))) end
    end
    cpu.__t, cpu.__b = total, busy
end, cpu)

local mem = wibox.widget.textbox()
watch([[sh -c "free -m | awk '/Mem:/ {printf \"%d %d\", $3, $2}'"]], 5, function(w, out)
    local used, total = out:match("(%d+) (%d+)")
    if used and total then
        w:set_markup(string.format('<span font="JetBrains Mono Bold 10">  %d%%</span>',
            math.floor(tonumber(used) / tonumber(total) * 100 + 0.5)))
    end
end, mem)

local vol = wibox.widget.textbox()
local function vol_update()
    read_async(
        [[wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null || pactl get-sink-volume @DEFAULT_SINK@ 2>/dev/null]],
        function(out)
            local v = out:match("Volume:%s*([%d%.]+)")
            local muted = out:match("MUTED") ~= nil
            local pct = v and math.floor(tonumber(v) * 100 + 0.5) or 0
            if v and tonumber(v) > 1.5 then pct = math.floor(tonumber(v) + 0.5) end
            local icon = muted and "" or (pct > 50 and "" or (pct > 0 and "" or ""))
            vol:set_markup(string.format('<span font="JetBrains Mono Bold 10">%s  %d%%</span>', icon, pct))
        end)
end
vol_update()
gears.timer({ timeout = 5, autostart = true, call_now = true, callback = vol_update })
vol:buttons(gears.table.join(
    awful.button({}, 1, function() awful.spawn("wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"); gears.timer.start_new(0.2, function() vol_update() end) end),
    awful.button({}, 3, function() awful.spawn("pavucontrol") end),
    awful.button({}, 4, function() awful.spawn("wpctl set-volume -l 1.4 @DEFAULT_AUDIO_SINK@ 5%+"); gears.timer.start_new(0.2, function() vol_update() end) end),
    awful.button({}, 5, function() awful.spawn("wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-"); gears.timer.start_new(0.2, function() vol_update() end) end)))

local bat = wibox.widget.textbox()
watch([[sh -c "cat /sys/class/power_supply/BAT*/capacity 2>/dev/null | head -1; cat /sys/class/power_supply/BAT*/status 2>/dev/null | head -1"]], 30,
    function(w, out)
        local cap, status = out:match("(%d+)%s*(%a*)")
        if cap then
            local icon = (status == "Charging") and "" or ""
            w:set_markup(string.format('<span font="JetBrains Mono Bold 10">%s  %s%%</span>', icon, cap))
        else
            w:set_markup("")
        end
    end, bat)

local power_btn = wibox.widget.textbox('<span font="JetBrains Mono Bold 11" color="' .. C.red .. '"></span>')
power_btn:buttons(gears.table.join(awful.button({}, 1, function() awful.spawn("de-powermenu") end)))

local launcher_btn = wibox.widget.textbox('<span font="JetBrains Mono Bold 13" color="' .. C.blue .. '"></span>')
launcher_btn:buttons(gears.table.join(
    awful.button({}, 1, function() awful.spawn(launcher) end),
    awful.button({}, 3, function() mymainmenu:toggle() end)))

-- ── wallpaper / screens ──────────────────────────────────────
local function set_wallpaper(s)
    if beautiful.wallpaper then
        local wp = beautiful.wallpaper
        if type(wp) == "function" then wp = wp(s) end
        gears.wallpaper.maximized(wp, s, true)
    end
end
screen.connect_signal("property::geometry", set_wallpaper)

local taglist_buttons = gears.table.join(
    awful.button({ }, 1, function(t) t:view_only() end),
    awful.button({ modkey }, 1, function(t) if client.focus then client.focus:move_to_tag(t) end end),
    awful.button({ }, 3, awful.tag.viewtoggle),
    awful.button({ }, 4, function(t) awful.tag.viewprev(t.screen) end),
    awful.button({ }, 5, function(t) awful.tag.viewnext(t.screen) end))

local tasklist_buttons = gears.table.join(
    awful.button({ }, 1, function (c)
        if c == client.focus then c.minimized = true
        else c:emit_signal("request::activate", "tasklist", {raise = true}) end end),
    awful.button({ }, 3, function (c) c:kill() end),
    awful.button({ }, 4, function () awful.client.focus.byidx(1) end),
    awful.button({ }, 5, function () awful.client.focus.byidx(-1) end))

awful.screen.connect_for_each_screen(function(s)
    set_wallpaper(s)
    awful.tag({ "1", "2", "3", "4", "5" }, s, awful.layout.layouts[1])

    s.mypromptbox = awful.widget.prompt()
    s.mylayoutbox = awful.widget.layoutbox(s)
    s.mylayoutbox:buttons(gears.table.join(
        awful.button({ }, 1, function () awful.layout.inc( 1) end),
        awful.button({ }, 3, function () awful.layout.inc(-1) end),
        awful.button({ }, 4, function () awful.layout.inc( 1) end),
        awful.button({ }, 5, function () awful.layout.inc(-1) end)))

    s.mytaglist = awful.widget.taglist {
        screen = s, filter = awful.widget.taglist.filter.all, buttons = taglist_buttons,
        style = { shape = function(cr,w,h) gears.shape.rounded_rect(cr,w,h,dpi(10)) end },
        layout = { spacing = dpi(4), layout = wibox.layout.fixed.horizontal },
        widget_template = {
            { { id = "text_role", widget = wibox.widget.textbox },
              left = dpi(10), right = dpi(10), widget = wibox.container.margin },
            id = "background_role", widget = wibox.container.background,
        },
    }

    s.mytasklist = awful.widget.tasklist {
        screen = s, filter = awful.widget.tasklist.filter.currenttags, buttons = tasklist_buttons,
        style = { shape = function(cr,w,h) gears.shape.rounded_rect(cr,w,h,dpi(10)) end },
        layout = { spacing = dpi(4), layout = wibox.layout.fixed.horizontal },
        widget_template = {
            { { { { id = "icon_role", widget = wibox.widget.imagebox },
                  margins = dpi(4), widget = wibox.container.margin },
                { id = "text_role", widget = wibox.widget.textbox },
                spacing = dpi(4), layout = wibox.layout.fixed.horizontal },
              left = dpi(8), right = dpi(12), widget = wibox.container.margin },
            id = "background_role", widget = wibox.container.background,
        },
    }

    s.mywibox = awful.wibar({ position = "top", screen = s, height = dpi(44), bg = "#00000000" })
    s.mywibox:setup {
        {
            {   -- left
                launcher_btn, s.mytaglist, s.mypromptbox,
                spacing = dpi(10), layout = wibox.layout.fixed.horizontal,
            },
            {   -- middle
                pill(clock, C.text), layout = wibox.layout.fixed.horizontal,
            },
            {   -- right
                pill(cpu, C.green), pill(mem, C.mauve), pill(vol, C.peach), pill(bat, C.teal),
                pill(wibox.widget { wibox.widget.systray(), layout = wibox.layout.fixed.horizontal }),
                pill(s.mylayoutbox, C.blue), pill(power_btn, C.red),
                spacing = dpi(6), layout = wibox.layout.fixed.horizontal,
            },
            expand = "none",
            layout = wibox.layout.align.horizontal,
        },
        left = dpi(12), right = dpi(12), top = dpi(6), bottom = dpi(2),
        widget = wibox.container.margin,
    }
end)

-- ── bindings ─────────────────────────────────────────────────
root.buttons(gears.table.join(
    awful.button({ }, 3, function () mymainmenu:toggle() end),
    awful.button({ }, 4, awful.tag.viewnext),
    awful.button({ }, 5, awful.tag.viewprev)))

globalkeys = gears.table.join(
    awful.key({ modkey }, "slash", hotkeys_popup.show_help, {description="show help", group="awesome"}),
    awful.key({ modkey }, "Left",  awful.tag.viewprev, {description="previous tag", group="tag"}),
    awful.key({ modkey }, "Right", awful.tag.viewnext, {description="next tag", group="tag"}),
    awful.key({ modkey }, "Escape", awful.tag.history.restore, {description="go back", group="tag"}),

    awful.key({ modkey }, "j", function () awful.client.focus.byidx( 1) end, {description="focus next", group="client"}),
    awful.key({ modkey }, "k", function () awful.client.focus.byidx(-1) end, {description="focus previous", group="client"}),
    awful.key({ modkey }, "Tab", function () awful.client.focus.history.previous()
        if client.focus then client.focus:raise() end end, {description="last client", group="client"}),

    awful.key({ modkey, "Shift" }, "j", function () awful.client.swap.byidx(  1) end, {description="swap next", group="client"}),
    awful.key({ modkey, "Shift" }, "k", function () awful.client.swap.byidx( -1) end, {description="swap previous", group="client"}),
    awful.key({ modkey, "Control" }, "j", function () awful.screen.focus_relative( 1) end, {description="next screen", group="screen"}),
    awful.key({ modkey, "Control" }, "k", function () awful.screen.focus_relative(-1) end, {description="previous screen", group="screen"}),

    awful.key({ modkey }, "Return", function () awful.spawn(terminal) end, {description="terminal", group="launcher"}),
    awful.key({ modkey }, "d", function () awful.spawn(launcher) end, {description="app launcher", group="launcher"}),
    awful.key({ modkey }, "e", function () awful.spawn("de-files") end, {description="file manager", group="launcher"}),
    awful.key({ modkey, "Shift" }, "e", function () awful.spawn("de-powermenu") end, {description="power menu", group="launcher"}),
    awful.key({ }, "Print", function () awful.spawn("de-screenshot") end, {description="screenshot", group="launcher"}),
    awful.key({ modkey, "Control" }, "r", awesome.restart, {description="reload awesome", group="awesome"}),
    awful.key({ modkey, "Shift" }, "q", awesome.quit, {description="quit awesome", group="awesome"}),

    awful.key({ modkey }, "l", function () awful.tag.incmwfact( 0.05) end, {description="grow master", group="layout"}),
    awful.key({ modkey }, "h", function () awful.tag.incmwfact(-0.05) end, {description="shrink master", group="layout"}),
    awful.key({ modkey, "Shift" }, "h", function () awful.tag.incnmaster( 1, nil, true) end, {description="more masters", group="layout"}),
    awful.key({ modkey, "Shift" }, "l", function () awful.tag.incnmaster(-1, nil, true) end, {description="fewer masters", group="layout"}),
    awful.key({ modkey }, "space", function () awful.layout.inc( 1) end, {description="next layout", group="layout"}),
    awful.key({ modkey, "Shift" }, "space", function () awful.layout.inc(-1) end, {description="previous layout", group="layout"}),
    awful.key({ modkey, "Control" }, "n", function ()
        local c = awful.client.restore()
        if c then c:emit_signal("request::activate", "key.unminimize", {raise = true}) end end,
        {description="restore minimized", group="client"}),

    awful.key({ modkey }, "r", function () awful.screen.focused().mypromptbox:run() end, {description="run prompt", group="launcher"}),
    awful.key({ modkey }, "p", function() menubar.show() end, {description="menubar", group="launcher"}),

    awful.key({ }, "XF86AudioRaiseVolume", function () awful.spawn("wpctl set-volume -l 1.4 @DEFAULT_AUDIO_SINK@ 5%+") end),
    awful.key({ }, "XF86AudioLowerVolume", function () awful.spawn("wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-") end),
    awful.key({ }, "XF86AudioMute", function () awful.spawn("wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle") end),
    awful.key({ }, "XF86MonBrightnessUp", function () awful.spawn("brightnessctl -e4 -n2 set 5%+") end),
    awful.key({ }, "XF86MonBrightnessDown", function () awful.spawn("brightnessctl -e4 -n2 set 5%-") end),
    awful.key({ }, "XF86AudioPlay", function () awful.spawn("playerctl play-pause") end),
    awful.key({ }, "XF86AudioNext", function () awful.spawn("playerctl next") end),
    awful.key({ }, "XF86AudioPrev", function () awful.spawn("playerctl previous") end)
)

clientkeys = gears.table.join(
    awful.key({ modkey }, "f", function (c) c.fullscreen = not c.fullscreen; c:raise() end, {description="fullscreen", group="client"}),
    awful.key({ modkey }, "q", function (c) c:kill() end, {description="close", group="client"}),
    awful.key({ modkey, "Control" }, "space", awful.client.floating.toggle, {description="toggle floating", group="client"}),
    awful.key({ modkey, "Control" }, "Return", function (c) c:swap(awful.client.getmaster()) end, {description="move to master", group="client"}),
    awful.key({ modkey }, "o", function (c) c:move_to_screen() end, {description="move to screen", group="client"}),
    awful.key({ modkey }, "t", function (c) c.ontop = not c.ontop end, {description="toggle on top", group="client"}),
    awful.key({ modkey }, "n", function (c) c.minimized = true end, {description="minimize", group="client"}),
    awful.key({ modkey }, "m", function (c) c.maximized = not c.maximized; c:raise() end, {description="maximize", group="client"})
)

for i = 1, 9 do
    globalkeys = gears.table.join(globalkeys,
        awful.key({ modkey }, "#" .. i + 9, function ()
                local screen = awful.screen.focused()
                local tag = screen.tags[i]
                if tag then tag:view_only() end end,
            {description = "view tag #"..i, group = "tag"}),
        awful.key({ modkey, "Control" }, "#" .. i + 9, function ()
                local screen = awful.screen.focused()
                local tag = screen.tags[i]
                if tag then awful.tag.viewtoggle(tag) end end,
            {description = "toggle tag #" .. i, group = "tag"}),
        awful.key({ modkey, "Shift" }, "#" .. i + 9, function ()
                if client.focus then
                    local tag = client.focus.screen.tags[i]
                    if tag then client.focus:move_to_tag(tag) end
                end end,
            {description = "move to tag #"..i, group = "tag"})
    )
end

clientbuttons = gears.table.join(
    awful.button({ }, 1, function (c) c:emit_signal("request::activate", "mouse_click", {raise = true}) end),
    awful.button({ modkey }, 1, function (c)
        c:emit_signal("request::activate", "mouse_click", {raise = true}); awful.mouse.client.move(c) end),
    awful.button({ modkey }, 3, function (c)
        c:emit_signal("request::activate", "mouse_click", {raise = true}); awful.mouse.client.resize(c) end))

root.keys(globalkeys)

-- ── rules ────────────────────────────────────────────────────
awful.rules.rules = {
    { rule = { },
      properties = { border_width = beautiful.border_width,
                     border_color = beautiful.border_normal,
                     focus = awful.client.focus.filter,
                     raise = true,
                     keys = clientkeys,
                     buttons = clientbuttons,
                     screen = awful.screen.preferred,
                     placement = awful.placement.no_overlap+awful.placement.no_offscreen,
                     shape = function(cr, w, h) gears.shape.rounded_rect(cr, w, h, dpi(10)) end }
    },
    { rule_any = {
        instance = { "DTA", "copyq", "pinentry" },
        class    = { "Arandr", "Blueman-manager", "Gpick", "Sxiv", "Tor Browser",
                     "Wpa_gui", "veromix", "xtightvncviewer", "Pavucontrol",
                     "Nm-connection-editor" },
        name     = { "Event Tester" },
        role     = { "AlarmWindow", "ConfigManager", "pop-up" },
      }, properties = { floating = true, placement = awful.placement.centered }},
    { rule_any = { type = { "normal", "dialog" } }, properties = { titlebars_enabled = false } },
    { rule_any = { type = { "dialog" } }, properties = { floating = true, placement = awful.placement.centered } },
}

-- ── signals ──────────────────────────────────────────────────
client.connect_signal("manage", function (c)
    if awesome.startup and not c.size_hints.user_position and not c.size_hints.program_position then
        awful.placement.no_offscreen(c)
    end
end)

client.connect_signal("mouse::enter", function(c)
    c:emit_signal("request::activate", "mouse_enter", {raise = false})
end)

client.connect_signal("focus",   function(c) c.border_color = beautiful.border_focus  end)
client.connect_signal("unfocus", function(c) c.border_color = beautiful.border_normal end)
