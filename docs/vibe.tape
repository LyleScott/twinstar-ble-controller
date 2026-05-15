# vhs tape that records the twinstar-vibe day-cycle preview to docs/vibe.gif.
#
# Renders one full day cycle in --no-send mode (no fixture required) at 4x
# speed, so the GIF is ~8 seconds and small enough to embed in the README.
#
# Regenerate with: vhs docs/vibe.tape

Output docs/vibe.gif

Set Shell      "bash"
Set FontSize   13
Set Width      720
Set Height     420
Set Padding    16
Set Theme      "Builtin Solarized Dark"
Set TypingSpeed 40ms

# Hidden setup: silence the prompt customization (venv activator,
# precmd hooks, theme glyphs) so only our command + the animation
# end up in the recording.
Hide
Type `export PS1='$ '; export PROMPT_COMMAND=''; unset VIRTUAL_ENV_PROMPT; clear`
Enter
Sleep 400ms
Show

Type "twinstar-vibe --no-send --speed 4 --cycles 1"
Sleep 400ms
Enter

# One full day cycle at speed 4 is 30/4 = 7.5s; pad a hair for startup
# and the final frame so the GIF doesn't end mid-sunset.
Sleep 8500ms
