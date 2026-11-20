"""Settings for the dashboard and the collector, read from .env next to this file.

.env holds KEY=value lines (# starts a comment line, quotes around a value are removed). A real environment
variable of the same name wins over the file. Every setting has a default, so .env may be missing or partial;
.env.example lists them all.
"""
import os

DIR = os.path.dirname(os.path.abspath(__file__))


def read_env(path):
    """{KEY: value} from a .env file; {} if it does not exist."""
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = (p.strip() for p in line.split("=", 1))
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                vals[key] = val
    except FileNotFoundError:
        pass
    return vals


_vals = read_env(os.path.join(DIR, ".env"))


def get(key, default, vals=None):
    """The setting `key` converted to the type of `default` (str, int, float or list of comma-separated strings)."""
    raw = os.environ.get(key, (_vals if vals is None else vals).get(key))
    if raw is None or raw == "":
        return default
    if isinstance(default, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(default, (int, float)):
        try:
            return type(default)(raw)
        except ValueError:
            raise SystemExit(f".env: {key}={raw!r} is not a number") from None
    return raw


def password():
    """FRITZ_PASSWORD, or the first line of FRITZ_PASSWORD_FILE."""
    pw = get("FRITZ_PASSWORD", "")
    if pw:
        return pw
    with open(os.path.expanduser(get("FRITZ_PASSWORD_FILE", "~/.claude_fritz"))) as f:
        return f.readline().strip()


def mesh_names(vals=None):
    """MESH_NAMES=box-name=Label,other-name=Label: display names for mesh nodes by their name in the box."""
    return dict(p.split("=", 1) for p in get("MESH_NAMES", [], vals) if "=" in p)


BOX = get("FRITZ_HOST", "192.168.178.1")
USER = get("FRITZ_USER", "claude")
