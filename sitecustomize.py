import os

# Temporary default credentials for the private Zakupay panel.
# Render environment variables, if configured, still take precedence.
os.environ.setdefault("PANEL_USERNAME", "mekhman")
os.environ.setdefault(
    "PANEL_PASSWORD_HASH",
    "pbkdf2_sha256$310000$8ALqwa3AllE8y5JRovQo5w$f5fmouYRWr3p6GinSHlmKD68yFj2UhcM75HJdjrEIVY",
)
