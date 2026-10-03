/**
 * The one place the host mount lines are written. The UI prints these verbatim
 * so what the user reads is what the deployment doc says (issue #13 item 8);
 * duplicating them per screen is how they drift apart.
 *
 * Paths are relative to `$HOME` and never written as absolute paths
 * (`docs/references.md`).
 */
export const KEY_FILE = '/keys/key_retail.bin'
export const KEY_DIR = '/keys/'
export const MACRO_LIBRARY = '/library/macros'
export const AMIIBO_LIBRARY = '/library/amiibo'

export const MOUNTS = {
  keyFile: `-v "$HOME/clone/Amiibo/!Essential Files/key_retail.bin":${KEY_FILE}:ro`,
  keyDir: `-v "$HOME/clone/Amiibo/'!Essential Files'":${KEY_DIR}:ro`,
  macros: `-v "$HOME/clone/switch-controller-macro/宏":${MACRO_LIBRARY}:ro`,
  amiibo: `-v "$HOME/clone/Amiibo":${AMIIBO_LIBRARY}:ro`,
} as const

export const FLASH_COMMAND = `docker compose stop controller && idf.py -p /dev/ttyACM0 flash`
