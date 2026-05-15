// Tells Firefox to read /usr/lib64/firefox/firefox.cfg at startup.
// Loaded from /usr/lib64/firefox/defaults/pref/autoconfig.js — the path is
// hard-coded into Firefox's startup. obscure_value=0 means firefox.cfg is
// plain JS (not byte-shifted), which is how everyone ships it in practice.
pref("general.config.filename", "firefox.cfg");
pref("general.config.obscure_value", 0);
pref("general.config.sandbox_enabled", false);
