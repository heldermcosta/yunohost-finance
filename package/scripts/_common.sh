#!/bin/bash
# Shared definitions for the finance package. Sourced by every script.
# App settings (install_dir, data_dir, port, db_name, db_user, db_pwd, ...) are loaded by YunoHost.

app=${YNH_APP_INSTANCE_NAME:-finance}

# Resource values are saved as app settings during provisioning, so read them here.
# This works in every script, including install, where the settings directory does not exist yet.
_finance_setting() { ynh_app_setting_get --app="$app" --key="$1"; }
install_dir=${install_dir:-$(_finance_setting install_dir)}
data_dir=${data_dir:-$(_finance_setting data_dir)}
port=${port:-$(_finance_setting port)}
db_name=${db_name:-$(_finance_setting db_name)}
db_user=${db_user:-$(_finance_setting db_user)}
db_pwd=${db_pwd:-$(_finance_setting db_pwd)}

src_dir="$install_dir/src"
venv_dir="$install_dir/venv"
env_file="$install_dir/app.env"
key_dir="/etc/$app"
key_file="$key_dir/secret.key"

finance_install_python() {
    # The app runs from a virtualenv so system Python packages stay untouched.
    python3 -m venv "$venv_dir"
    "$venv_dir/bin/pip" install --upgrade pip --quiet
    "$venv_dir/bin/pip" install --requirement "$src_dir/requirements.txt" --quiet
}

finance_write_env() {
    # The database password is read from the YunoHost-managed setting db_pwd.
    cat > "$env_file" <<ENV
FINANCE_DATA_DIR=$data_dir
FINANCE_WORK_DIR=$data_dir/work
FINANCE_DATABASE_URL=postgresql+psycopg://$db_user:$db_pwd@localhost:5432/$db_name
FINANCE_KEY_FILE=$key_file
ENV
    chown "root:$app" "$env_file"
    chmod 0640 "$env_file"
}

finance_ensure_key() {
    # The key lives in /etc/<app>, outside the install and data dirs, and is not part of backups.
    # A new install creates it. Restores require the key from the original server (see restore).
    install -d -m 0750 -o root -g "$app" "$key_dir"
    if [ ! -f "$key_file" ]; then
        (cd "$src_dir" && PYTHONPATH="$src_dir" "$venv_dir/bin/python" -c \
            "from pathlib import Path; from app.crypto import load_or_create_key; load_or_create_key(Path('$key_file'))")
    fi
    chown "root:$app" "$key_file"
    chmod 0440 "$key_file"
}
