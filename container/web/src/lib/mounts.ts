/**
 * The one place the host mount lines are written. The UI prints these verbatim
 * so what the user reads is what the deployment doc says (issue #13 item 8);
 * duplicating them per screen is how they drift apart.
 *
 * Every path starts at the corpus root, `${REFERENCE_ROOT:-$HOME/clone}` — the
 * spelling and default §10.4 deploys with and `docs/references.md` resolves —
 * and is never an absolute path (`docs/references.md`). The `\$` is a template
 * literal's escape, not part of the line the user copies.
 *
 * The key sits *inside* the `Amiibo Bin/` tree, not beside it (#43): the path
 * beside it does not fail, it makes Docker create a directory, and the
 * container then reports `KEY_ABSENT` for a key the user did mount.
 */
export const KEY_FILE = '/keys/key_retail.bin'
export const KEY_DIR = '/keys/'
export const MACRO_LIBRARY = '/library/macros'
export const AMIIBO_LIBRARY = '/library/amiibo'

export const MOUNTS = {
  keyFile: `-v "\${REFERENCE_ROOT:-$HOME/clone}/Amiibo/Amiibo Bin/!Essential Files/key_retail.bin":${KEY_FILE}:ro`,
  keyDir: `-v "\${REFERENCE_ROOT:-$HOME/clone}/Amiibo/Amiibo Bin/!Essential Files":${KEY_DIR}:ro`,
  macros: `-v "\${REFERENCE_ROOT:-$HOME/clone}/switch-controller-macro/宏":${MACRO_LIBRARY}:ro`,
  amiibo: `-v "\${REFERENCE_ROOT:-$HOME/clone}/Amiibo":${AMIIBO_LIBRARY}:ro`,
} as const

export const FLASH_COMMAND = `docker compose stop controller && idf.py -p /dev/ttyACM0 flash`
