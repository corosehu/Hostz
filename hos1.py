#coded by @avesz @corose
import subprocess
import sys
import importlib.util


def install_if_missing(packages: dict):
    """
    Checks if packages are installed and installs them if they are missing.
    :param packages: A dictionary where keys are pip package names
                     and values are the names used for import.
    """
    for package_name, import_name in packages.items():
        # Check if the module can be imported
        should_install = False
        if import_name and importlib.util.find_spec(import_name) is None:
            should_install = True

        # Special check for python-telegram-bot job queue, which requires apscheduler
        if package_name == "python-telegram-bot" and importlib.util.find_spec("apscheduler") is None:
            if not should_install:
                # This message is useful if 'telegram' is installed but 'apscheduler' is not
                print("JobQueue support for python-telegram-bot not found. Upgrading...")
            should_install = True

        if should_install:
            print(f"Installing/upgrading package: {package_name}...")
            try:
                # Special case for python-telegram-bot with job-queue
                if package_name == "python-telegram-bot":
                    install_command = [
                        sys.executable, "-m", "pip", "install", "--upgrade", "python-telegram-bot[job-queue]"
                    ]
                else:
                    install_command = [sys.executable, "-m", "pip", "install", package_name]

                subprocess.check_call(install_command)
                print(f"Successfully installed/upgraded {package_name}.")
            except subprocess.CalledProcessError as e:
                print(f"Failed to install {package_name}. Error: {e}", file=sys.stderr)
                print("Please install it manually and restart the bot.", file=sys.stderr)
                sys.exit(1)  # Exit if a critical dependency fails to install.


# List of required third-party packages and their import names
# Format: { 'pip-package-name': 'import_name' }
required_packages = {
    "python-telegram-bot": "telegram",
    "dropbox": "dropbox",
    "psutil": "psutil",
    "httpx": "httpx",
}

install_if_missing(required_packages)

print("All required modules are installed and ready.")

import os
import logging
import json
import time
import threading
import subprocess
import psutil
import platform
import asyncio
import signal
import uuid
import zipfile
import shutil
import tempfile
import httpx
import pty
import fcntl
import select
import dropbox
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import functools
import telegram

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
    from telegram.constants import ParseMode
except ImportError:
    # For older versions
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, ParseMode
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=LOG_LEVEL,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Suppress verbose logs from libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram.ext').setLevel(logging.WARNING)


async def _send_admin_notification(context: ContextTypes.DEFAULT_TYPE):
    """Callback job to send a message to admins."""
    job_context = context.job.data
    message = job_context["message"]
    admin_ids = job_context["admin_ids"]
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=message, parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"Failed to send notification to admin {admin_id}: {e}")


async def _edit_admin_notification(context: ContextTypes.DEFAULT_TYPE):
    """Callback job to edit a message for an admin."""
    logger.info("Executing _edit_admin_notification job")
    job_context = context.job.data
    chat_id = job_context["chat_id"]
    message_id = job_context["message_id"]
    new_text = job_context["text"]
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info(f"Successfully edited message {message_id} in chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to edit notification for chat {chat_id}: {e}")


# Bot configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "7320676748:AAHofgkz35-MgY_LCJ_w7iCIkJ2bnMgdg0k")
ADMIN_IDS = [6827291977, 5349091019]  # Add your Telegram user ID here
SCRIPTS_DIR = "bot_scripts"
LOGS_DIR = "bot_logs"
DATA_FILE = "bot_data.json"
BACKUP_DIR = "backups"
DROPBOX_CONFIG_FILE = "dropbox_config.json"


class ScriptManager:
    def __init__(self, application: "Application"):
        self.application = application
        self.scripts: Dict[str, Dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.script_stdin_pipes: Dict[str, subprocess.Popen] = {}
        self.terminal_sessions: Dict[int, Dict] = {}
        self.interactive_processes: Dict[int, subprocess.Popen] = {}
        self.backup_thread = None
        self.last_backup_time = None
        self._data_lock = threading.Lock()  # Lock for thread-safe data saving
        self.dropbox_config = self.load_dropbox_config()
        self.load_data()
        self.ensure_directories()
        self.monitor_thread = threading.Thread(target=self.monitor_processes, daemon=True)
        self.monitor_thread.start()
        self.start_backup_scheduler()

    def load_dropbox_config(self) -> Dict:
        """Loads Dropbox config from a JSON file."""
        config_path = Path(DROPBOX_CONFIG_FILE)
        if not config_path.exists():
            logger.info("Dropbox config file not found.")
            return {}
        try:
            with config_path.open('r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Error: Dropbox config file at '{config_path}' is not a valid JSON.")
        except PermissionError:
            logger.error(f"Error: Permission denied when reading '{config_path}'.")
        except Exception as e:
            logger.error(f"An unexpected error occurred while loading Dropbox config: {e}")
        return {}

    def save_dropbox_config(self):
        """Saves Dropbox config to a JSON file with secure permissions."""
        try:
            with self._data_lock:
                config_path = Path(DROPBOX_CONFIG_FILE)
                with config_path.open('w') as f:
                    json.dump(self.dropbox_config, f, indent=2)
                # Set secure permissions (read/write for owner only)
                config_path.chmod(0o600)
        except Exception as e:
            logger.error(f"Error saving Dropbox config: {e}")

    def ensure_directories(self):
        """Create necessary directories using pathlib."""
        base_path = Path.cwd()
        directories = [
            base_path / SCRIPTS_DIR,
            base_path / LOGS_DIR,
            base_path / "temp_uploads",
            base_path / BACKUP_DIR,
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o755)
            logger.info(f"✅ Directory ready: {directory}")

        # Clean up old temp files (older than 1 hour)
        try:
            temp_dir = base_path / "temp_uploads"
            current_time = time.time()
            cleaned_count = 0
            for file_path in temp_dir.iterdir():
                if file_path.is_file():
                    if current_time - file_path.stat().st_mtime > 3600:  # 1 hour
                        file_path.unlink()
                        cleaned_count += 1
            if cleaned_count > 0:
                logger.info(f"🧹 Cleaned up {cleaned_count} old temp files")
        except Exception as e:
            logger.warning(f"Error cleaning temp files: {e}")

    def load_data(self):
        """Load persistent data"""
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    self.scripts = data.get('scripts', {})
                    self.last_backup_time = data.get('last_backup_time', None)
                    logger.info(f"Loaded {len(self.scripts)} scripts from data file")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            self.scripts = {}

    def save_data(self):
        """Save persistent data in a thread-safe manner."""
        with self._data_lock:
            try:
                data = {
                    'scripts': self.scripts,
                    'last_updated': datetime.now().isoformat(),
                    'last_backup_time': self.last_backup_time
                }
                # Use a temporary file and atomic rename for safer writes
                temp_file_path = f"{DATA_FILE}.tmp"
                with open(temp_file_path, 'w') as f:
                    json.dump(data, f, indent=2)

                # Atomically move the temporary file to the final destination
                shutil.move(temp_file_path, DATA_FILE)

            except Exception as e:
                logger.error(f"Error saving data: {e}")

    def add_script(self, file_path: str, original_name: str, script_type: str) -> str:
        """Add a new script using pathlib."""
        script_id = str(uuid.uuid4())[:8]
        unique_name = f"{script_id}_{original_name}"
        
        scripts_dir = Path(SCRIPTS_DIR).resolve()
        final_path = scripts_dir / unique_name

        script_info = {
            'id': script_id,
            'original_name': original_name,
            'file_name': unique_name,
            'file_path': str(final_path),
            'script_type': script_type,
            'created_at': datetime.now().isoformat(),
            'status': 'stopped',
            'auto_restart': False,
            'restart_count': 0,
            'last_started': None,
            'last_stopped': None,
        }
        
        try:
            source_path = Path(file_path)
            source_path.rename(final_path)
            final_path.chmod(0o755)
            logger.info(f"Script moved to: {final_path}")
        except Exception as e:
            logger.error(f"Error moving script file: {e}")
            raise
        
        self.scripts[script_id] = script_info
        self.save_data()
        
        logger.info(f"Added script: {original_name} with ID: {script_id}")
        return script_id

    def start_backup_scheduler(self):
        """Start the automatic backup scheduler"""
        def backup_scheduler():
            while True:
                try:
                    # Calculate next backup time (24 hours from last backup or now)
                    now = datetime.now()
                    if self.last_backup_time:
                        last_backup = datetime.fromisoformat(self.last_backup_time)
                        next_backup = last_backup + timedelta(hours=24)
                        if now >= next_backup:
                            self.create_automatic_backup()
                    else:
                        # First time, schedule backup for next 24 hours
                        self.last_backup_time = now.isoformat()
                        self.save_data()
                    
                    # Check every hour
                    time.sleep(3600)
                except Exception as e:
                    logger.error(f"Error in backup scheduler: {e}")
                    time.sleep(3600)
        
        self.backup_thread = threading.Thread(target=backup_scheduler, daemon=True)
        self.backup_thread.start()
        logger.info("📅 Automatic backup scheduler started")

    def create_backup(self, is_automatic=False):
        """Create a complete backup using pathlib."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_type = "auto" if is_automatic else "manual"
            backup_filename = f"bot_backup_{backup_type}_{timestamp}.zip"
            backup_path = Path(BACKUP_DIR) / backup_filename

            logger.info(f"🔄 Creating {backup_type} backup: {backup_filename}")

            with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Add bot_data.json
                data_file = Path(DATA_FILE)
                if data_file.exists():
                    zipf.write(data_file, data_file.name)
                    logger.info(f"✅ Added {data_file.name} to backup")

                # Add directories
                for dir_name in [SCRIPTS_DIR, LOGS_DIR]:
                    dir_path = Path(dir_name)
                    if dir_path.exists():
                        for file_path in dir_path.rglob('*'):
                            if file_path.is_file():
                                # The arcname should be the path relative to the project root.
                                # Since file_path is already a relative path, it can be used directly.
                                zipf.write(file_path, file_path)
                        logger.info(f"✅ Added {dir_name} directory to backup")

                # Add bot.log
                log_file = Path('bot.log')
                if log_file.exists():
                    zipf.write(log_file, log_file.name)
                    logger.info("✅ Added bot.log to backup")

                # Add backup metadata
                metadata = {
                    'backup_type': backup_type,
                    'created_at': datetime.now().isoformat(),
                    'scripts_count': len(self.scripts),
                    'running_scripts': len([s for s in self.scripts.values() if s.get('status') == 'running']),
                    'bot_version': '2.1',
                    'platform': platform.system(),
                }
                zipf.writestr('backup_metadata.json', json.dumps(metadata, indent=2))
                logger.info("✅ Added backup metadata")

            self.last_backup_time = datetime.now().isoformat()
            self.save_data()
            self.cleanup_old_backups()

            backup_size = backup_path.stat().st_size / 1024
            logger.info(f"✅ Backup created: {backup_filename} ({backup_size:.1f} KB)")
            return True, f"Backup created: {backup_filename} ({backup_size:.1f} KB)", str(backup_path)

        except Exception as e:
            logger.error(f"❌ Error creating backup: {e}")
            return False, f"Error creating backup: {str(e)}", None

    def create_automatic_backup(self):
        """Create automatic backup and upload to Dropbox"""
        try:
            success, message, path = self.create_backup(is_automatic=True)
            if success:
                logger.info(f"✅ Automatic backup completed: {message}")
                # Upload to Dropbox in a new thread to avoid blocking
                if path:
                    threading.Thread(target=self.upload_to_dropbox, args=(path, False)).start()
            else:
                logger.error(f"❌ Automatic backup failed: {message}")
        except Exception as e:
            logger.error(f"Error in automatic backup: {e}")

    def get_dropbox_client(self) -> Optional[dropbox.Dropbox]:
        """Creates a Dropbox client, refreshing the token if necessary."""
        config = self.dropbox_config
        required_keys = ["app_key", "app_secret", "refresh_token"]
        missing_keys = [key for key in required_keys if not config.get(key)]
        if missing_keys:
            logger.error(f"Dropbox is not configured. Missing required keys: {', '.join(missing_keys)}. Please use /setup_dropbox.")
            return None

        if time.time() >= config.get("expires_at", 0) - 60:
            logger.info("Dropbox access token has expired or is about to. Refreshing...")
            success, message = self._refresh_dropbox_token()
            if not success:
                # Log the specific error from the refresh attempt
                logger.error(f"Failed to refresh Dropbox token during client creation: {message}")
                # Potentially notify admin about persistent failure here
                return None

        if not config.get("access_token"):
            logger.error("Dropbox access token is missing. Cannot create client.")
            return None

        return dropbox.Dropbox(config["access_token"])

    def _refresh_dropbox_token(self) -> Tuple[bool, str]:
        """
        Refreshes the Dropbox access token using the stored refresh_token.
        This is a helper method to be used by other public methods.
        Returns (success: bool, message: str for logging/debugging)
        """
        config = self.dropbox_config
        required_keys = ["app_key", "app_secret", "refresh_token"]

        # Validate config
        if not all(k in config and config[k] for k in required_keys):
            return False, "Dropbox not configured. Missing required keys."

        try:
            logger.info("Refreshing Dropbox access token...")
            dbx = dropbox.Dropbox(
                app_key=config["app_key"],
                app_secret=config["app_secret"],
                oauth2_refresh_token=config["refresh_token"],
            )
            dbx.refresh_access_token()
            config["access_token"] = dbx._oauth2_access_token
            config["expires_at"] = time.time() + dbx._oauth2_access_token_expiration
            self.save_dropbox_config()
            logger.info("Dropbox token refreshed successfully.")
            return True, "Token refreshed successfully."
        except dropbox.exceptions.AuthError as e:
            error_msg = f"Authentication error: {e}. The refresh token may be invalid or revoked."
            logger.error(f"Dropbox token refresh failed. {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = f"An unexpected error occurred: {e}"
            logger.error(f"Dropbox token refresh failed. {error_msg}", exc_info=True)
            return False, error_msg

    def refresh_dropbox_token(self) -> Tuple[bool, str]:
        """
        Manually refresh Dropbox access token using the stored refresh_token.
        Returns (success: bool, message: str for user)
        """
        logger.info("🔄 Manually refreshing Dropbox access token...")
        success, message = self._refresh_dropbox_token()
        if success:
            return True, "✅ Dropbox access token refreshed successfully!"
        else:
            # Provide a user-friendly error message
            if "not configured" in message:
                 return False, "❌ Dropbox not configured. Run `/setup_dropbox` and `/dropbox_code` first."
            return False, f"❌ Failed to refresh Dropbox token: {message}"

    def upload_to_dropbox(
        self,
        backup_path_str: str,
        manual_export: bool = False,
        chat_id: int = None,
        message_id: int = None,
    ):
        """Uploads a backup file to Dropbox and notifies the admin."""
        # Get a descriptive error message if the client fails to initialize
        try:
            dbx = self.get_dropbox_client()
            if not dbx:
                # Check config for a more specific error
                config = self.dropbox_config
                required_keys = ["app_key", "app_secret", "refresh_token"]
                missing_keys = [key for key in required_keys if not config.get(key)]
                if missing_keys:
                    error_reason = f"Missing config keys: {', '.join(missing_keys)}"
                else:
                    error_reason = "Token refresh failed. Check logs for details."

                success = False
                result = f"Dropbox client initialization failed. Reason: {error_reason}"
            else:
                # Proceed with upload
                backup_path = Path(backup_path_str)
                dropbox_path = f"/BotBackups/{backup_path.name}"
                with backup_path.open("rb") as f:
                    dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode('overwrite'))

                link = dbx.sharing_create_shared_link_with_settings(dropbox_path)
                success = True
                result = link.url.replace("?dl=0", "?dl=1")

        except dropbox.exceptions.ApiError as e:
            if e.error.is_shared_link_already_exists():
                links = dbx.sharing_list_shared_links(path=dropbox_path).links
                if links:
                    success = True
                    result = links[0].url.replace("?dl=0", "?dl=1")
                else:
                    success = False
                    result = "Failed to retrieve existing shared link."
            else:
                success = False
                result = f"Dropbox API error: {e}"
        except Exception as e:
            success = False
            result = f"An unexpected error occurred: {e}"

        backup_path = Path(backup_path_str)
        if success:
            download_link = result
            backup_type = "Manual" if manual_export else "Automatic"
            message = (
                f"✅ *{backup_type} Backup Uploaded*\n\n"
                f"*File:* `{escape_markdown(backup_path.name)}`\n"
                f"*Link:* [Direct Download]({download_link})\n\n"
                f"Restore using `/importlink`\\."
            )
        else:
            error_details = result
            backup_type = "Manual" if manual_export else "Automatic"
            message = (
                f"❌ *Dropbox Upload Failed*\n\n"
                f"*File:* `{escape_markdown(backup_path.name)}`\n"
                f"*Error:* `{escape_markdown(error_details)}`"
            )

        job_data = {
            "text": message,
            "chat_id": chat_id,
            "message_id": message_id,
        } if chat_id and message_id else {
            "message": message,
            "admin_ids": ADMIN_IDS
        }

        callback = _edit_admin_notification if chat_id and message_id else _send_admin_notification
        self.application.job_queue.run_once(callback, 0, data=job_data)
        logger.info("Scheduled Dropbox backup notification.")

    def cleanup_old_backups(self, keep_count=10):
        """Clean up old backup files, keeping the most recent ones."""
        try:
            backup_dir = Path(BACKUP_DIR)
            if not backup_dir.exists():
                return
            
            backup_files = sorted(
                (p for p in backup_dir.glob('bot_backup_*.zip') if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            
            for file_to_delete in backup_files[keep_count:]:
                try:
                    file_to_delete.unlink()
                except Exception as e:
                    logger.warning(f"Could not remove old backup {file_to_delete}: {e}")
            
            if len(backup_files) > keep_count:
                logger.info(f"🧹 Cleaned up {len(backup_files) - keep_count} old backups")
                
        except Exception as e:
            logger.warning(f"Error cleaning up old backups: {e}")

    def restore_backup(self, backup_file_path_str: str):
        """Restore bot data from a backup file."""
        backup_path = Path(backup_file_path_str)
        try:
            logger.info(f"🔄 Starting backup restoration from: {backup_path.name}")
            
            if not backup_path.is_file():
                return False, "Backup file not found."
            
            if not zipfile.is_zipfile(backup_path):
                return False, "Invalid backup file format (not a ZIP)."
            
            # Stop all running scripts
            stopped_count = self.stop_all_scripts()
            logger.info(f"🛑 Stopped {stopped_count} running scripts for restore.")
            
            with tempfile.TemporaryDirectory(prefix='restore_') as temp_dir_str:
                temp_dir = Path(temp_dir_str)
                
                with zipfile.ZipFile(backup_path, 'r') as zipf:
                    if not any(f.startswith((DATA_FILE, SCRIPTS_DIR)) for f in zipf.namelist()):
                        return False, "Backup file seems invalid (missing key components)."
                    zipf.extractall(temp_dir)
                    logger.info("📦 Extracted backup to temporary directory.")

                # Restore files and directories
                for item in [DATA_FILE, SCRIPTS_DIR, LOGS_DIR]:
                    source = temp_dir / item
                    dest = Path(item)
                    if source.exists():
                        if dest.exists():
                            backup_dest = Path(f"{dest}_{int(time.time())}.bak")
                            dest.rename(backup_dest)
                            logger.info(f"Backed up current '{dest}' to '{backup_dest}'")
                        shutil.copytree(source, dest) if source.is_dir() else shutil.copy2(source, dest)
                        logger.info(f"✅ Restored '{dest}'")
                
                # Reload data and normalize paths
                self.load_data()
                self.processes.clear()
                self.script_stdin_pipes.clear()

                scripts_dir_abs = Path(SCRIPTS_DIR).resolve()
                for script_id, script_info in self.scripts.items():
                    script_info.update({
                        'status': 'stopped',
                        'last_stopped': datetime.now().isoformat(),
                        'pid': None,
                        'file_path': str(scripts_dir_abs / script_info['file_name'])
                    })
                
                self.save_data()
                
                valid_scripts = sum(1 for s in self.scripts.values() if Path(s['file_path']).exists())
                logger.info("✅ Backup restoration completed successfully.")
                return True, f"Restored {len(self.scripts)} scripts ({valid_scripts} valid)."
                    
        except zipfile.BadZipFile:
            return False, "Invalid or corrupted ZIP file."
        except Exception as e:
            logger.error(f"❌ Error during backup restoration: {e}", exc_info=True)
            return False, f"Restoration failed: {str(e)}"

    def get_run_command(self, script_info: Dict) -> List[str]:
        """Get the appropriate run command for script type"""
        file_path = script_info['file_path']
        script_type = script_info['script_type']
        
        # Use just the filename since working directory is set to script directory
        filename = os.path.basename(file_path)
        
        if script_type == 'python':
            return [sys.executable, filename]
        elif script_type == 'shell':
            return ['bash', filename]
        elif script_type == 'javascript':
            return ['node', filename]
        else:
            return ['bash', filename]  # Default to bash

    def start_script(self, script_id: str) -> Tuple[bool, str]:
        """Start a script."""
        if script_id not in self.scripts:
            return False, "Script not found"
        
        script_info = self.scripts[script_id]
        script_path = Path(script_info['file_path'])

        if script_id in self.processes and self.processes[script_id].poll() is None:
            return False, "Script is already running"
        
        if not script_path.exists():
            logger.error(f"Script file not found: {script_path}")
            return False, f"Script file not found: {script_path.name}"
        
        try:
            log_path = Path(LOGS_DIR).resolve() / f"{script_id}.log"
            
            with log_path.open('a') as log_file:
                log_file.write(f"\n--- Started at {datetime.now().isoformat()} ---\n")
                
                cmd = self.get_run_command(script_info)
                logger.info(f"Running command: {' '.join(cmd)} in {script_path.parent}")
                
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=script_path.parent,
                    preexec_fn=os.setsid,
                    text=True,
                    bufsize=1,
                )
                
                self.processes[script_id] = process
                self.script_stdin_pipes[script_id] = process
                self.scripts[script_id].update({
                    'status': 'running',
                    'last_started': datetime.now().isoformat(),
                    'pid': process.pid,
                })
                self.save_data()
                
                logger.info(f"Started script {script_id} with PID {process.pid}")
                return True, f"Script started successfully (PID: {process.pid})"
                
        except Exception as e:
            logger.error(f"Error starting script {script_id}: {e}")
            return False, f"Error starting script: {str(e)}"

    def stop_script(self, script_id: str) -> Tuple[bool, str]:
        """Stop a script."""
        if script_id not in self.scripts:
            return False, "Script not found"
        
        process = self.processes.get(script_id)
        if not process or process.poll() is not None:
            self.scripts[script_id]['status'] = 'stopped'
            self.save_data()
            return True, "Script was not running."
        
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(pgid, signal.SIGKILL)
                process.wait(timeout=2)
            except Exception as e:
                logger.warning(f"Failed to force kill script {script_id}: {e}")
        except Exception as e:
            logger.error(f"Error stopping script {script_id}: {e}")
            return False, f"Error stopping script: {str(e)}"

        del self.processes[script_id]
        if script_id in self.script_stdin_pipes:
            del self.script_stdin_pipes[script_id]

        self.scripts[script_id].update({
            'status': 'stopped',
            'last_stopped': datetime.now().isoformat(),
            'pid': None,
        })
        self.save_data()

        logger.info(f"Stopped script: {self.scripts[script_id]['original_name']} (ID: {script_id})")
        return True, f"Script '{self.scripts[script_id]['original_name']}' stopped."

    def stop_all_scripts(self):
        """
        Stops all currently running scripts.
        Iterates over a copy of the process dictionary keys to allow for safe
        modification during iteration.
        """
        stopped_count = sum(1 for script_id in list(self.processes.keys()) if self.stop_script(script_id)[0])
        logger.info(f"Stopped {stopped_count} scripts.")
        return stopped_count

    def restart_script(self, script_id: str) -> Tuple[bool, str]:
        """Restart a script."""
        if script_id not in self.scripts:
            return False, "Script not found"
        
        self.stop_script(script_id)
        time.sleep(1)
        return self.start_script(script_id)

    def delete_script(self, script_id: str) -> Tuple[bool, str]:
        """Delete a script and its associated files."""
        if script_id not in self.scripts:
            return False, "Script not found"
        
        script = self.scripts[script_id]
        
        try:
            self.stop_script(script_id)
            
            script_path = Path(script['file_path'])
            if script_path.exists():
                script_path.unlink()
            
            log_file = Path(LOGS_DIR) / f"{script_id}.log"
            if log_file.exists():
                log_file.unlink()
            
            del self.scripts[script_id]
            self.save_data()
            
            logger.info(f"Deleted script: {script['original_name']} (ID: {script_id})")
            return True, f"Script '{script['original_name']}' deleted."
            
        except Exception as e:
            logger.error(f"Error deleting script {script_id}: {e}")
            return False, f"Error deleting script: {str(e)}"

    def send_input_to_script(self, script_id: str, input_text: str) -> Tuple[bool, str]:
        """Send input to a running script."""
        if script_id not in self.script_stdin_pipes:
            return False, "Script not running or doesn't accept input."
        
        try:
            process = self.script_stdin_pipes[script_id]
            if process.poll() is None:
                process.stdin.write(input_text + '\n')
                process.stdin.flush()
                return True, "Input sent."
            else:
                return False, "Script has terminated."
                
        except Exception as e:
            logger.error(f"Error sending input to script {script_id}: {e}")
            return False, f"Error sending input: {str(e)}"

    def send_input_to_script_by_pid(self, pid: int, input_text: str) -> Tuple[bool, str]:
        """Send input to a script by PID."""
        target_script_id = next((sid for sid, s in self.scripts.items() if s.get('pid') == pid), None)

        if not target_script_id:
            return False, f"No managed script found with PID {pid}"

        return self.send_input_to_script(target_script_id, input_text)

    def get_script_logs(self, script_id: str, lines: int = 50) -> str:
        """Get recent logs for a script."""
        if script_id not in self.scripts:
            return "Script not found."
        
        log_file = Path(LOGS_DIR) / f"{script_id}.log"
        
        if not log_file.exists():
            return "No logs available for this script."
        
        try:
            with log_file.open('r') as f:
                log_lines = f.readlines()
            return ''.join(log_lines[-lines:])
        except Exception as e:
            return f"Error reading logs: {e}"

    def get_system_info(self) -> str:
        """Get system information"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            info = f"""🖥️ **System Information**
            
**CPU Usage:** {cpu_percent}%
**Memory:** {memory.percent}% used ({memory.used // 1024 // 1024} MB / {memory.total // 1024 // 1024} MB)
**Disk:** {disk.percent}% used ({disk.used // 1024 // 1024 // 1024} GB / {disk.total // 1024 // 1024 // 1024} GB)
**Platform:** {platform.system()} {platform.release()}
**Python:** {platform.python_version()}

**Running Scripts:** {len([s for s in self.scripts.values() if s.get('status') == 'running'])}
**Total Scripts:** {len(self.scripts)}
            """
            return info
        except Exception as e:
            return f"Error getting system info: {e}"

    def list_scripts(self) -> List[Dict]:
        """Get list of all scripts"""
        return list(self.scripts.values())

    def get_running_processes(self):
        """Get list of running processes"""
        try:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                try:
                    processes.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return processes
        except Exception as e:
            logger.error(f"Error getting processes: {e}")
            return []

    def kill_process(self, pid: int) -> Tuple[bool, str]:
        """Kill a process by PID"""
        try:
            process = psutil.Process(pid)
            process.terminate()
            return True, f"Process {pid} terminated successfully"
        except psutil.NoSuchProcess:
            return False, f"Process {pid} not found"
        except psutil.AccessDenied:
            return False, f"Permission denied to kill process {pid}"
        except Exception as e:
            return False, f"Error killing process {pid}: {str(e)}"

    def monitor_processes(self):
        """Monitor running scripts and restart if needed"""
        while True:
            try:
                for script_id, process in list(self.processes.items()):
                    if process.poll() is not None:  # Process has terminated
                        script = self.scripts.get(script_id)
                        if script:
                            script['status'] = 'stopped'
                            script['last_stopped'] = datetime.now().isoformat()
                            script.pop('pid', None)
                            
                            # Auto restart if enabled
                            if script.get('auto_restart', False):
                                logger.info(f"Auto-restarting script: {script['original_name']}")
                                self.start_script(script_id)
                            
                        # Clean up
                        del self.processes[script_id]
                        if script_id in self.script_stdin_pipes:
                            del self.script_stdin_pipes[script_id]
                
                self.save_data()
                time.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                logger.error(f"Error in process monitor: {e}")
                time.sleep(30)

    def toggle_auto_restart(self, script_id: str) -> Tuple[bool, str]:
        """Toggle auto-restart for a script"""
        if script_id not in self.scripts:
            return False, "Script not found"
        
        script = self.scripts[script_id]
        current_state = script.get('auto_restart', False)
        script['auto_restart'] = not current_state
        self.save_data()
        
        new_state = "enabled" if script['auto_restart'] else "disabled"
        return True, f"Auto-restart {new_state} for '{script['original_name']}'"

    def execute_terminal_command(self, user_id: int, command: str) -> str:
        """Execute a terminal command safely."""
        try:
            # Security: Avoid executing as shell. Split command into arguments.
            args = command.split()
            if not args:
                return "No command entered."

            # A simple blocklist is not a robust security measure, but can prevent common mistakes.
            # The removal of shell=True is the primary security enhancement.
            dangerous_commands = ['rm', 'dd', 'mkfs', 'sudo']
            if args[0] in dangerous_commands and '-rf' in args:
                return "⚠️ Command blocked for safety reasons."

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                check=False  # Do not raise exception on non-zero exit codes
            )
            
            output = result.stdout
            if result.stderr:
                output += f"\n--- STDERR ---\n{result.stderr}"
            
            return output if output.strip() else "Command executed (no output)."
            
        except subprocess.TimeoutExpired:
            return "⏱️ Command timed out (30s limit)."
        except FileNotFoundError:
            return f"❌ Command not found: {args[0]}"
        except Exception as e:
            return f"❌ Error executing command: {str(e)}"

    def start_interactive_terminal(self, user_id: int) -> Tuple[bool, str]:
        """Start interactive terminal session using a PTY."""
        if user_id in self.interactive_processes:
            return False, "Terminal session is already active."

        try:
            # Create a pseudo-terminal
            master_fd, slave_fd = pty.openpty()

            # Start a new bash session in the PTY
            process = subprocess.Popen(
                ['bash', '-i'],
                preexec_fn=os.setsid,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                text=True,
                bufsize=1,
                close_fds=True
            )
            
            # Close the slave descriptor in the parent
            os.close(slave_fd)

            # Make the master descriptor non-blocking
            fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

            self.interactive_processes[user_id] = {
                'process': process,
                'master_fd': master_fd
            }

            logger.info(f"Started PTY-based interactive terminal for user {user_id} with PID {process.pid}")
            return True, "Interactive terminal started."
            
        except Exception as e:
            logger.error(f"Error starting PTY terminal: {e}")
            return False, f"Error starting PTY terminal: {str(e)}"

    def stop_interactive_terminal(self, user_id: int) -> Tuple[bool, str]:
        """Stop interactive terminal session and clean up resources."""
        if user_id not in self.interactive_processes:
            return False, "No active terminal session."

        try:
            session = self.interactive_processes[user_id]
            process = session['process']
            master_fd = session['master_fd']

            # Terminate the process
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=2)

            # Close the master file descriptor
            os.close(master_fd)

            del self.interactive_processes[user_id]
            logger.info(f"Stopped interactive terminal for user {user_id}")
            return True, "Interactive terminal stopped."

        except Exception as e:
            logger.error(f"Error stopping PTY terminal: {e}")
            # Ensure cleanup
            if user_id in self.interactive_processes:
                del self.interactive_processes[user_id]
            return False, f"Error stopping terminal: {str(e)}"

    def send_input_to_terminal(self, user_id: int, input_text: str, add_newline: bool = True) -> Tuple[bool, str]:
        """Send input to the PTY-based interactive terminal."""
        if user_id not in self.interactive_processes:
            return False, "No active terminal session."

        try:
            session = self.interactive_processes[user_id]
            if session['process'].poll() is not None:
                self.stop_interactive_terminal(user_id)
                return False, "Terminal session has ended. Please restart."
            
            master_fd = session['master_fd']
            
            if add_newline:
                input_text += '\n'
            
            os.write(master_fd, input_text.encode())
            return True, "Input sent."
            
        except Exception as e:
            logger.error(f"Error sending input to PTY: {e}")
            return False, f"Error sending input: {str(e)}"

    def read_terminal_output(self, user_id: int, timeout: float = 0.5) -> str:
        """Read output from the PTY-based interactive terminal."""
        if user_id not in self.interactive_processes:
            return "No active terminal session."

        try:
            session = self.interactive_processes[user_id]
            if session['process'].poll() is not None:
                self.stop_interactive_terminal(user_id)
                return "Terminal session has ended. Please restart."

            master_fd = session['master_fd']
            
            # Use select to wait for data to be available for reading
            ready_to_read, _, _ = select.select([master_fd], [], [], timeout)
            
            if ready_to_read:
                output = ""
                while True:
                    try:
                        chunk = os.read(master_fd, 1024)
                        if not chunk:
                            break
                        output += chunk.decode(errors='ignore')
                    except BlockingIOError:
                        # No more data to read at the moment
                        break
                return output if output else "No output received."
            else:
                return "No output received."
                
        except Exception as e:
            logger.error(f"Error reading PTY output: {e}")
            return f"Error reading output: {str(e)}"

    def get_dropbox_status(self) -> str:
        """Checks the Dropbox configuration and returns a status message."""
        status = ["*🔍 Dropbox Integration Status*"]
        config_path = Path(DROPBOX_CONFIG_FILE)

        # 1. Check for config file existence and permissions
        if not config_path.exists():
            status.append("  \\- ❌ *Config File:* Not found at `dropbox_config\\.json`\\.")
            return "\n".join(status)

        status.append(f"  \\- ✅ *Config File:* Found at `{escape_markdown(str(config_path))}`\\.")

        try:
            # This will trigger a read and catch permission errors
            config = self.load_dropbox_config()
            if not config: # Handles JSON error or other read issues
                 status.append("  \\- ❌ *Config Access:* Could not be read\\. Check logs for permission or JSON format errors\\.")
                 return "\n".join(status)

        except Exception as e:
            status.append(f"  \\- ❌ *Config Access:* An unexpected error occurred: {e}")
            return "\n".join(status)

        # 2. Check for required fields
        required_keys = ["app_key", "app_secret", "refresh_token"]
        missing_keys = [key for key in required_keys if not config.get(key)]

        if missing_keys:
            status.append(f"  \\- ❌ *Required Fields:* Missing `{', '.join(missing_keys)}`\\.")
        else:
            status.append("  \\- ✅ *Required Fields:* All present \\(app\\_key, app\\_secret, refresh\\_token\\)\\.")

        # 3. Check token status
        if not config.get("refresh_token"):
            status.append("  \\- 🟡 *Token Status:* Needs setup\\. Use `/setup_dropbox`\\.")
        elif time.time() >= config.get("expires_at", 0) - 60:
            status.append("  \\- 🟡 *Token Status:* Expired\\. Will attempt refresh on next backup\\.")
        else:
            expires_in = timedelta(seconds=int(config.get("expires_at", 0) - time.time()))
            status.append(f"  \\- ✅ *Token Status:* Access token is valid \\(expires in {escape_markdown(str(expires_in))}\\)\\.")

        # 4. Attempt to connect to Dropbox
        try:
            dbx = self.get_dropbox_client()
            if dbx:
                dbx.users_get_current_account()
                status.append("  \\- ✅ *API Connection:* Successfully connected to Dropbox API\\.")
            else:
                status.append("  \\- ❌ *API Connection:* Failed to initialize Dropbox client\\. Check logs\\.")
        except Exception as e:
            status.append(f"  \\- ❌ *API Connection:* Failed with error: `{escape_markdown(str(e))}`\\.")

        return "\n".join(status)


def escape_markdown(text: str) -> str:
    """
    Escapes special characters for Telegram's MarkdownV2 parse mode.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{char}" if char in escape_chars else char for char in text)


def resilient_api_call(func):
    """Decorator to retry Telegram API calls only on specific, recoverable network errors."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return await func(*args, **kwargs)
            except telegram.error.NetworkError as e:
                if attempt == 2:
                    logger.error(f"Telegram API call failed after 3 attempts due to network issues: {e}")
                    raise  # Re-raise the final NetworkError
                logger.warning(f"Network error in {func.__name__}, attempt {attempt + 1}: {e}. Retrying...")
                await asyncio.sleep(2)  # Increased delay for better recovery
            except Exception as e:
                # This is not a recoverable network error, so log it and re-raise immediately.
                logger.error(f"Unrecoverable API error in {func.__name__}: {e}")
                raise e
    return wrapper


class TelegramBot:
    def __init__(self):
        # Unify Application instance creation in __init__
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.script_manager = ScriptManager(self.application)

    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in ADMIN_IDS

    @resilient_api_call
    async def unauthorized_response(self, update: Update):
        """Send unauthorized response"""
        await update.message.reply_text("🚫 Unauthorized access. Contact admin.")
        logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log the error and send a telegram message to notify the developer."""
        logger.error("Exception while handling an update:", exc_info=context.error)

    @resilient_api_call
    async def send_script_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send input to a specific script"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            if len(context.args) < 2:
                await update.message.reply_text(
                    "❌ Usage: `/sinput <script_id> <input_text>`\n\n"
                    "Example: `/sinput abc123 mypassword`\n"
                    "Use `/scripts` to see script IDs"
                )
                return
            
            script_id = context.args[0]
            input_text = ' '.join(context.args[1:])
            
            success, message = self.script_manager.send_input_to_script(script_id, input_text)
            
            if success:
                await update.message.reply_text(f"✅ Input sent to script `{escape_markdown(script_id)}`: `{escape_markdown(input_text)}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_script_input: {e}")
            await update.message.reply_text(f"❌ *Error sending input:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def send_pid_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send input to a script by PID"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            if len(context.args) < 2:
                await update.message.reply_text(
                    "❌ Usage: `/pinput <pid> <input_text>`\n\n"
                    "Example: `/pinput 1234 mypassword`\n"
                    "Use `/scripts` to see script PIDs"
                )
                return
            
            try:
                pid = int(context.args[0])
            except ValueError:
                await update.message.reply_text("❌ Invalid PID. Please provide a number.")
                return
            
            input_text = ' '.join(context.args[1:])
            
            success, message = self.script_manager.send_input_to_script_by_pid(pid, input_text)
            
            if success:
                await update.message.reply_text(f"✅ Input sent to PID `{pid}`: `{escape_markdown(input_text)}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_pid_input: {e}")
            await update.message.reply_text(f"❌ *Error sending input:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def send_enter_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send input with Enter key to terminal"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            # Check if in terminal mode
            if user_id not in self.script_manager.terminal_sessions:
                await update.message.reply_text("❌ Terminal mode not active. Use /terminal to enable.")
                return
            
            # Get input text
            input_text = ' '.join(context.args) if context.args else ""
            
            # Send input to terminal
            success, message = self.script_manager.send_input_to_terminal(user_id, input_text, add_newline=True)
            
            if success:
                await update.message.reply_text(f"📝 Input sent: {input_text}")
                
                # Wait a moment and get output
                await asyncio.sleep(1)
                output = self.script_manager.read_terminal_output(user_id, timeout=3.0)
                
                if output and output != "No output received":
                    # Truncate if too long
                    if len(output) > 4000:
                        output = output[:4000] + "\n\n... (output truncated)"
                    
                    await update.message.reply_text(f"```\n{output}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_enter_input: {e}")
            await update.message.reply_text(f"❌ *Error sending input:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def send_space(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send space key to terminal"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            if user_id not in self.script_manager.terminal_sessions:
                await update.message.reply_text("❌ *Terminal mode not active\\.*", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            success, message = self.script_manager.send_input_to_terminal(user_id, " ", add_newline=False)
            
            if success:
                await update.message.reply_text("⌨️ *Space key sent*", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_space: {e}")
            await update.message.reply_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def send_ctrl_c(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send Ctrl+C to terminal"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            if user_id not in self.script_manager.terminal_sessions:
                await update.message.reply_text("❌ Terminal mode not active.")
                return
            
            success, message = self.script_manager.send_input_to_terminal(user_id, "\x03", add_newline=False)  # Ctrl+C
            
            if success:
                await update.message.reply_text("🛑 *Ctrl+C sent (interrupt signal)*")
                
                # Get output after interrupt
                await asyncio.sleep(1)
                output = self.script_manager.read_terminal_output(user_id, timeout=2.0)
                if output and output != "No output received":
                    await update.message.reply_text(f"```\n{output}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_ctrl_c: {e}")
            await update.message.reply_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def send_raw_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send raw input without Enter to terminal"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            if user_id not in self.script_manager.terminal_sessions:
                await update.message.reply_text("❌ Terminal mode not active.")
                return
            
            if not context.args:
                await update.message.reply_text("❌ Please provide input text. Usage: `/input your text here`")
                return
            
            input_text = ' '.join(context.args)
            success, message = self.script_manager.send_input_to_terminal(user_id, input_text, add_newline=False)
            
            if success:
                await update.message.reply_text(f"📝 Raw input sent: {input_text}")
            else:
                await update.message.reply_text(f"❌ {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in send_raw_input: {e}")
            await update.message.reply_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Simple test command to verify bot is working"""
        try:
            logger.info(f"📨 Test command from user {update.effective_user.id}")
            
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
                
            await update.message.reply_text("✅ Bot is working! All systems operational.")
            logger.info("✅ Test command completed successfully")
            
        except Exception as e:
            logger.error(f"❌ Test command error: {e}")
            try:
                await update.message.reply_text(f"❌ Error: {e}")
            except:
                pass

    @resilient_api_call
    async def import_from_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Import a backup from a direct download link without blocking."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        if not context.args:
            await update.message.reply_text(
                "❌ *Usage:* `/importlink <direct_download_url>`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        url = context.args[0]
        if not ("dropbox.com" in url and "dl=1" in url):
            await update.message.reply_text(
                "❌ *Invalid URL:* Please provide a Dropbox direct download link \\(`?dl=1`\\)\\.", parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        processing_msg = await update.message.reply_text("⬇️ Downloading backup file...")
        temp_file_path = None

        try:
            # Download and save the file
            async with httpx.AsyncClient() as client:
                response = await client.get(url, follow_redirects=True, timeout=60.0)
                response.raise_for_status()

            temp_dir = os.path.abspath("temp_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            temp_file_path = os.path.join(temp_dir, f"restore_{uuid.uuid4().hex}.zip")

            # Write file asynchronously if possible, but standard `open` is blocking
            # For simplicity, we accept this small blocking operation, or use a thread
            await asyncio.to_thread(Path(temp_file_path).write_bytes, response.content)

            await processing_msg.edit_text("📦 Backup downloaded. Verifying...")

            # Run blocking operations in a thread
            def _restore_blocking_part():
                if not zipfile.is_zipfile(temp_file_path):
                    raise ValueError("Downloaded file is not a valid .zip archive.")

                # Pre-restore backup
                self.script_manager.create_backup(is_automatic=True)

                # Restore from backup
                return self.script_manager.restore_backup(temp_file_path)

            await processing_msg.edit_text("⚙️ Restoring from backup...")
            restore_success, restore_message = await asyncio.to_thread(_restore_blocking_part)

            if restore_success:
                await processing_msg.edit_text(f"✅ *Backup Restored\\!*\n\n{escape_markdown(restore_message)}", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await processing_msg.edit_text(f"❌ *Restore Failed:*\n\n{escape_markdown(restore_message)}", parse_mode=ParseMode.MARKDOWN_V2)

        except httpx.RequestError as e:
            await processing_msg.edit_text(f"❌ *Download Failed:* {escape_markdown(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        except ValueError as e:
            await processing_msg.edit_text(f"❌ *Invalid File:* {escape_markdown(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error during import from link: {e}")
            await processing_msg.edit_text(f"❌ *An unexpected error occurred:* {escape_markdown(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    @resilient_api_call
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        try:
            logger.info(f"📨 START command from user {update.effective_user.id}")
            
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
                
            welcome_text = """
🤖 *Enhanced Advanced Hosting Management Bot*

🚀 *Features:*
• Upload and run scripts \\(\\.py, \\.sh, \\.js\\)
• Real\\-time interactive terminal access
• Background process management
• *Script\\-specific input support*
• Auto\-restart capabilities
• System monitoring
• Log management
• Backup/restore functionality

📋 *Commands:*
• `/scripts` \- Manage your scripts
• `/status` \- Server status
• `/terminal` \- Toggle terminal mode
• `/cmd <command>` \- Execute shell command
• `/ps` \- List running processes
• `/kill <pid>` \- Kill process by PID
• `/export` \- Create local backup
• `/importlink <url>` \- Restore from Dropbox link

🖥️ *Terminal Input Commands:*
• `/enter <text>` \- Send input \+ Enter key
• `/space` \- Send space key
• `/ctrl_c` \- Send Ctrl\+C \(interrupt\)
• `/input <text>` \- Send raw input \(no Enter\)

🎯 *Script Input Commands:*
• `/sinput <script_id> <text>` \- Send input to specific script
• `/pinput <pid> <text>` \- Send input to script by PID

💡 *Quick Start:*
1\. Upload a script file
2\. Use inline buttons to manage it
3\. Use script input commands for interactive scripts
4\. Toggle terminal mode for direct shell access

Your enhanced server is ready\! 🎯
            """
            
            keyboard = [
                [InlineKeyboardButton("📂 My Scripts", callback_data="list_scripts")],
                [InlineKeyboardButton("📊 Server Status", callback_data="server_status")],
                [InlineKeyboardButton("🖥️ Terminal Mode", callback_data="toggle_terminal")],
                [InlineKeyboardButton("📦 Backup Menu", callback_data="backup_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info("✅ Start command completed")
            
        except Exception as e:
            logger.error(f"❌ Start command error: {e}")
            try:
                await update.message.reply_text(f"Error: {e}")
            except:
                pass

    @resilient_api_call
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command handler"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            help_text = """
🤖 *Enhanced Advanced Hosting Bot — Complete Guide*

Your Telegram\\-powered server manager with script\\-specific input, interactive terminal, and Dropbox backup\\.

\\-\\-\\-

📁 *Script Management*
• Upload \\.py, \\.sh, or \\.js files → auto\\-detected & saved with unique ID
• Scripts start stopped by default
• Manage via inline buttons or commands:
  • ▶️ Start \\| ⏹️ Stop \\| 🔄 Restart
  • 📋 View Logs \\| 🗑️ Delete
  • 🔄 Toggle Auto\\-restart \\(crash recovery\\)

🎯 *Script\\-Specific Input \\(Core Feature\\!\\)*
> Send input directly to any running script — even with multiple scripts active\\!
• `/sinput <script_id> <text>`
  → e\\.g\\., `/sinput abc123 my2FACode`
• `/pinput <pid> <text>`
  → e\\.g\\., `/pinput 5678 confirm`
✅ Works independently of terminal mode
✅ Ideal for Instagram bots, auth prompts, interactive scripts

\\-\\-\\-

🖥️ *Interactive Terminal Mode*
• `/terminal` → toggle PTY\\-based interactive shell \\(no freezing\\!\\)
• In terminal mode: every message \\= shell command
• Special input commands \\(work anytime, inside or outside terminal\\):
  • `/enter <text>` → send \\+ press Enter \\(for passwords\\)
  • `/space` → send space key
  • `/ctrl_c` → send interrupt \\(Ctrl\\+C\\)
  • `/input <text>` → send raw text \\(no Enter\\)

💡 *Pro Tip:* Use `/terminal` for system tasks, and `/sinput` for script prompts — both work together\\!

\\-\\-\\-

📊 *System & Process Control*
• `/status` → CPU, RAM, disk, uptime, script count
• `/ps` → list top resource\\-consuming processes
• `/kill <pid>` → terminate any process
• Auto\\-monitoring: crashed scripts logged & optionally restarted

\\-\\-\\-

📦 *Backup & Restore \\(Dropbox Integrated\\)*

🔐 *Setup \\(One\\-Time\\):*
1\\. Create Dropbox app → get APP\\_KEY & APP\\_SECRET
2\\. Run: `/setup_dropbox <key> <secret>`
3\\. Open auth link → copy code
4\\. Run: `/dropbox_code <code>`
✅ Done\\! Backups auto\\-upload to `/BotBackups/`

🔄 *Commands:*
• `/export` → create manual backup → auto\\-upload to Dropbox
  ⚠️ Requires Dropbox setup
• `/importlink <url>` → restore full bot state
  ✅ URL must be Dropbox direct link ending with `?dl=1`
  ✅ Example: `https://www\\.dropbox\\.com/s/xxx/backup\\.zip?dl=1`

🛡️ *Safety Features:*
• Pre\\-restore backup created automatically before every `/importlink`
• Daily auto\\-backups \\(uploaded to Dropbox\\)
• Keeps last 10 backups locally \\(/backups/\\)
• Full data preserved: scripts, logs, settings, metadata

\\-\\-\\-

🔧 *Other Commands*
• `/cmd <command>` → run single shell command \\(non\\-interactive, 60s timeout\\)
• `/scripts` → list all scripts with ID, PID, status, and input\\-ready indicator
• `/test` → verify bot is responsive

\\-\\-\\-

💡 *Best Practices*
1\\. Always run `/export` before major changes
2\\. Use `/sinput` for Instagram/2FA/password prompts
3\\. Prefer `/terminal` \\+ `/ctrl_c` for graceful process stops
4\\. After `/importlink`, all scripts are stopped — start them manually
5\\. Keep Dropbox links private \\(they grant full bot restore access\\)

\\-\\-\\-

🔒 *Security*
• Admin\\-only access \\(your ID: 6827291977 , 5349091019\\)
• All actions logged to `bot.log`
• File operations sandboxed in `bot_scripts/` and `bot_logs/`
• No shell injection — commands run safely via subprocess \\(no `shell=True`\\)
• Dropbox tokens stored with `chmod 600`

\\-\\-\\-

🚀 *Perfect For:*
• Instagram automation \\(IG Expert workflows\\)
• Crypto trading bots
• Web scrapers with login prompts
• Node\\.js / Python microservices
• Any script needing real\\-time interaction

Your server\\. Fully automated\\. Fully interactive\\. Fully yours\\.
"""
            await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in help command: {e}")
            try:
                await update.message.reply_text(f"❌ Error occurred: {str(e)}")
            except:
                pass

    @resilient_api_call
    async def server_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get comprehensive server status"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            status_parts = []
            
            # System metrics with error handling
            try:
                cpu_percent = psutil.cpu_percent(interval=1)
                status_parts.append(f"• *CPU:* {escape_markdown(f'{cpu_percent}%')} usage")
            except Exception as e:
                status_parts.append(f"• *CPU:* Unable to read \\({escape_markdown(str(e)[:30])}\\.\\.\\.\\)")
            
            try:
                memory = psutil.virtual_memory()
                status_parts.append(f"• *Memory:* {escape_markdown(f'{memory.percent}%')} \\({escape_markdown(f'{memory.used // (1024**3)}GB / {memory.total // (1024**3)}GB')}\\)")
            except Exception as e:
                status_parts.append(f"• *Memory:* Unable to read \\({escape_markdown(str(e)[:30])}\\.\\.\\.\\)")
            
            try:
                disk = psutil.disk_usage('/')
                status_parts.append(f"• *Disk:* {escape_markdown(f'{disk.percent}%')} \\({escape_markdown(f'{disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB')}\\)")
            except Exception as e:
                status_parts.append(f"• *Disk:* Unable to read \\({escape_markdown(str(e)[:30])}\\.\\.\\.\\)")
                
            try:
                boot_time = datetime.fromtimestamp(psutil.boot_time())
                boot_time_str = boot_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                boot_time_str = "Unable to read"
            
            # Running scripts count
            try:
                running_scripts = len([s for s in self.script_manager.list_scripts() if s['status'] == 'running'])
                total_scripts = len(self.script_manager.scripts)
                scripts_with_input = len(self.script_manager.script_stdin_pipes)
            except Exception:
                running_scripts = 0
                total_scripts = 0
                scripts_with_input = 0
            
            # Active terminal sessions
            active_terminals = len(self.script_manager.interactive_processes)
            
            # System info with error handling
            try:
                system_info = {
                    'platform': platform.system(),
                    'release': platform.release(),
                    'architecture': platform.machine(),
                }
            except Exception:
                system_info = {
                    'platform': 'Unknown',
                    'release': 'Unknown', 
                    'architecture': 'Unknown'
                }
            
            # Network interfaces with error handling
            try:
                network_info = psutil.net_io_counters()
                network_sent = network_info.bytes_sent // (1024**2)
                network_recv = network_info.bytes_recv // (1024**2)
            except Exception:
                network_sent = 0
                network_recv = 0
            
            status_text = f"""*📊 Enhanced Server Status*

*🖥️ System:*
• *OS:* {escape_markdown(system_info['platform'])} {escape_markdown(system_info['release'])}
• *Architecture:* {escape_markdown(system_info['architecture'])}
• *Boot Time:* {escape_markdown(boot_time_str)}

*⚡ Performance:*
{chr(10).join(status_parts)}

*🔄 Scripts Status:*
• *Running:* {running_scripts}/{total_scripts}
• *Interactive Ready:* {scripts_with_input}
• *Total Managed:* {total_scripts}

*🖥️ Terminal Sessions:*
• *Active Interactive:* {active_terminals}

*🌐 Network:*
• *Bytes Sent:* {network_sent}MB
• *Bytes Received:* {network_recv}MB

*🔋 Health:* 🟢 Enhanced & Operational"""
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="server_status")],
                [InlineKeyboardButton("📂 Scripts", callback_data="list_scripts")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(status_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in server_status: {e}")
            try:
                await update.message.reply_text(f"❌ *Error getting server status:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    @resilient_api_call
    async def list_scripts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all managed scripts"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            scripts = self.script_manager.list_scripts()
            
            if not scripts:
                keyboard = [[InlineKeyboardButton("📤 Upload Script", callback_data="upload_help")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "📂 *No scripts found*\n\nUpload a `\\.py`, `\\.sh`, or `\\.js` file to get started\\!",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
            
            text = "📂 *Your Enhanced Scripts:*\n\n"
            keyboard = []
            
            for script in sorted(scripts, key=lambda x: x['created_at'], reverse=True):
                status_emoji = "🟢" if script['status'] == 'running' else "🔴"
                auto_restart_emoji = "🔄" if script.get('auto_restart', False) else ""
                input_ready_emoji = "🎯" if script['id'] in self.script_manager.script_stdin_pipes else ""
                
                text += f"{status_emoji} *{escape_markdown(script['original_name'])}* {auto_restart_emoji}{input_ready_emoji}\n"
                text += f"   • *Type:* `{escape_markdown(script['script_type'])}`\n"
                text += f"   • *Status:* {escape_markdown(script['status'])}\n"
                if script.get('pid'):
                    text += f"   • *PID:* `{script['pid']}`\n"
                text += f"   • *ID:* `{script['id']}`\n"
                if input_ready_emoji:
                    text += f"   • *Input Ready:* `/sinput {script['id']} <text>`\n"
                text += "\n"
                
                # Create buttons for each script
                keyboard.append([
                    InlineKeyboardButton(f"⚙️ {script['original_name'][:15]}", 
                                       callback_data=f"manage_{script['id']}")
                ])
            
            # Add legend
            text += "🎯 \\= Input Ready \\| 🔄 \\= Auto\\-restart \\| 🟢 \\= Running\n"
            
            # Add general buttons
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="list_scripts")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in list_scripts: {e}")
            try:
                await update.message.reply_text(f"❌ *Error listing scripts:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    @resilient_api_call
    async def export_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create and send manual backup, with a pre-check for Dropbox config."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        # Pre-emptive check for Dropbox configuration
        if not self.script_manager.get_dropbox_client():
            await update.message.reply_text(
                "❌ *Dropbox Not Configured*\n"
                "Please set up Dropbox integration first using `/setup_dropbox`\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        processing_msg = await update.message.reply_text("🔄 Creating backup...")

        try:
            success, message, backup_path = await asyncio.to_thread(
                self.script_manager.create_backup, is_automatic=False
            )

            if not success:
                await processing_msg.edit_text(f"❌ *Backup creation failed:* `{escape_markdown(message)}`", parse_mode=ParseMode.MARKDOWN_V2)
                return

            await processing_msg.edit_text("📤 Uploading backup to Dropbox...")

            await asyncio.to_thread(
                self.script_manager.upload_to_dropbox,
                backup_path,
                manual_export=True,
                chat_id=processing_msg.chat_id,
                message_id=processing_msg.message_id,
            )
        except Exception as e:
            logger.error(f"Error creating manual backup: {e}")
            await update.message.reply_text(f"❌ *An unexpected error occurred:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


    @resilient_api_call
    async def toggle_terminal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle enhanced interactive terminal mode"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            if user_id in self.script_manager.terminal_sessions:
                # Stop interactive terminal
                self.script_manager.stop_interactive_terminal(user_id)
                del self.script_manager.terminal_sessions[user_id]
                
                await update.message.reply_text(
                    "🖥️ *Interactive Terminal Disabled*\n\n"
                    "✅ Terminal session ended\n"
                    "🔙 Back to normal bot mode\n\n"
                    "💡 *Script input commands still available:*\n"
                    "• `/sinput <script_id> <text>`\n"
                    "• `/pinput <pid> <text>`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                # Start interactive terminal
                success, message = self.script_manager.start_interactive_terminal(user_id)
                
                if success:
                    self.script_manager.terminal_sessions[user_id] = {
                        'enabled': True,
                        'started_at': datetime.now().isoformat()
                    }
                    
                    await update.message.reply_text(
                        "🖥️ *Interactive Terminal Enabled*\n\n"
                        "✅ Terminal session started\n"
                        "📝 Every message \\= shell command\n"
                        "⌨️ *Input Commands Available:*\n"
                        "• `/enter <text>` \\- Send input \\+ Enter\n"
                        "• `/space` \\- Send space key\n"
                        "• `/ctrl_c` \\- Send Ctrl\\+C\n"
                        "• `/input <text>` \\- Send raw input\n\n"
                        "🎯 *Script Input Still Works:*\n"
                        "• `/sinput <script_id> <text>`\n"
                        "• `/pinput <pid> <text>`\n\n"
                        "🚨 *Enhanced:* No more freezing issues\\!\n"
                        "Type `/terminal` again to disable\\."
                    , parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await update.message.reply_text(f"❌ *Failed to start terminal:* `{escape_markdown(message)}`", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in toggle_terminal: {e}")
            try:
                await update.message.reply_text(f"❌ Error toggling terminal: {str(e)}")
            except:
                pass

    @resilient_api_call
    async def execute_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute a shell command - ENHANCED NON-FREEZING VERSION"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            if not context.args:
                await update.message.reply_text("❌ *Please provide a command\\.* Example: `/cmd ls \\-la`", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            command = ' '.join(context.args)
            await self.run_shell_command_safe(update, command)
            
        except Exception as e:
            logger.error(f"Error in execute_command: {e}")
            try:
                await update.message.reply_text(f"❌ *Error executing command:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def run_shell_command_safe(self, update: Update, command: str):
        """Run a shell command safely with timeout and proper output escaping."""
        try:
            processing_msg = await update.message.reply_text(f"🔄 *Executing:* `{escape_markdown(command)}`", parse_mode=ParseMode.MARKDOWN_V2)

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                await processing_msg.edit_text(f"⏰ *Command timed out:* `{escape_markdown(command)}`", parse_mode=ParseMode.MARKDOWN_V2)
                return

            output = ""
            if stdout:
                output += stdout.decode('utf-8', errors='ignore')
            if stderr:
                output += f"\n\\-\\-\\- STDERR \\-\\-\\-\n{stderr.decode('utf-8', errors='ignore')}"

            if not output.strip():
                output = "Command executed successfully (no output)."

            response_text = f"*Command:* `{escape_markdown(command)}`\n"
            response_text += f"*Exit Code:* `{process.returncode}`\n\n"

            # Truncate output if too long
            if len(output) > 3800:
                output = output[:3800] + "\n\n\\.\\.\\. \\(output truncated\\)"

            # Send as code block
            response_text += f"```\n{output}\n```"
            
            try:
                await processing_msg.edit_text(response_text, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                # If markdown fails, send as plain text
                logger.warning(f"Markdown send failed, sending as plain text. Error: {e}")
                await processing_msg.edit_text(f"Command: {command}\nExit Code: {process.returncode}\n\n{output}")

        except Exception as e:
            logger.error(f"Error in run_shell_command_safe: {e}")
            await update.message.reply_text(f"❌ *Command execution failed:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def list_processes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List running processes"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            processes = []
            try:
                for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                    try:
                        proc_info = proc.info
                        if proc_info['cpu_percent'] > 0 or proc_info['memory_percent'] > 0.1:
                            processes.append(proc_info)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
            except Exception as e:
                await update.message.reply_text(f"❌ *Error accessing process list:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            # Sort by CPU usage
            processes.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)
            processes = processes[:20]  # Top 20
            
            if not processes:
                text = "🔄 *No active processes found*\n\nThis may be due to system permission restrictions\\."
            else:
                text = "🔄 *Top Running Processes:*\n\n"
                for proc in processes:
                    cpu = proc.get('cpu_percent', 0)
                    mem = proc.get('memory_percent', 0)
                    name = proc.get('name', 'Unknown')
                    pid = proc.get('pid', 'Unknown')
                    text += f"• *PID* `{pid}`: `{escape_markdown(name)}`\n"
                    text += f"  *CPU:* {escape_markdown(f'{cpu:.1f}%')} \\| *RAM:* {escape_markdown(f'{mem:.1f}%')}\n\n"
            
            keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="list_processes")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in list_processes: {e}")
            try:
                await update.message.reply_text(f"❌ *Error listing processes:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    @resilient_api_call
    async def kill_process(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Kill a process by PID"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            if not context.args:
                await update.message.reply_text("❌ *Please provide a PID\\.* Example: `/kill 1234`", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            try:
                pid = int(context.args[0])
            except ValueError:
                await update.message.reply_text("❌ *Invalid PID\\.* Please provide a number\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            try:
                process = psutil.Process(pid)
                process_name = process.name()
                
                process.terminate()
                await update.message.reply_text(f"✅ *Process killed:* `{escape_markdown(process_name)}` \\(`{pid}`\\)", parse_mode=ParseMode.MARKDOWN_V2)
                
            except psutil.NoSuchProcess:
                await update.message.reply_text("❌ *Process not found\\.*", parse_mode=ParseMode.MARKDOWN_V2)
            except psutil.AccessDenied:
                await update.message.reply_text("❌ *Access denied\\.* Cannot kill this process \\(insufficient permissions\\)\\.", parse_mode=ParseMode.MARKDOWN_V2)
            except psutil.ZombieProcess:
                await update.message.reply_text("❌ *Cannot kill zombie process\\.*", parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                await update.message.reply_text(f"❌ *Error killing process:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in kill_process: {e}")
            await update.message.reply_text(f"❌ *Error killing process:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle file uploads without blocking the event loop."""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return

            processing_msg = await update.message.reply_text("📤 Processing your file...")

            file = await update.message.document.get_file()
            file_name = update.message.document.file_name

            # Determine script type
            if file_name.endswith('.py'):
                script_type = 'python'
            elif file_name.endswith('.sh'):
                script_type = 'shell'
            elif file_name.endswith('.js'):
                script_type = 'javascript'
            else:
                await processing_msg.edit_text(
                    "❌ *Unsupported file type*\\. Supported: `\\.py`, `\\.sh`, `\\.js`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return

            await processing_msg.edit_text("⬇️ Downloading file...")

            temp_dir = os.path.abspath("temp_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}_{file_name}")

            # Download in a non-blocking way
            await file.download_to_drive(temp_path)

            await processing_msg.edit_text("⚙️ Setting up script...")

            # Run the blocking script addition in a separate thread
            try:
                script_id = await asyncio.to_thread(
                    self.script_manager.add_script, temp_path, file_name, script_type
                )
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                await processing_msg.edit_text(f"❌ *Script setup failed:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
                return

            script_info = self.script_manager.scripts.get(script_id)
            if not script_info or not os.path.exists(script_info['file_path']):
                await processing_msg.edit_text("❌ *Script file validation failed*", parse_mode=ParseMode.MARKDOWN_V2)
                return

            keyboard = [
                [InlineKeyboardButton("▶️ Start", callback_data=f"start_{script_id}")],
                [InlineKeyboardButton("⚙️ Manage", callback_data=f"manage_{script_id}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            success_text = f"""✅ *Script uploaded successfully\\!*

📄 *File:* `{escape_markdown(file_name)}`
🆔 *ID:* `{script_id}`
🔧 *Type:* `{escape_markdown(script_type)}`
📁 *Location:* `{escape_markdown(SCRIPTS_DIR)}`
🎯 *Input Ready:* `/sinput {script_id} <text>`

Ready to run\\! 🚀"""

            await processing_msg.edit_text(success_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        except Exception as e:
            logger.error(f"Error handling document: {e}")
            try:
                await update.message.reply_text(f"❌ *Upload failed:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except Exception:
                pass


    @resilient_api_call
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages - ENHANCED TERMINAL MODE"""
        try:
            if not self.is_admin(update.effective_user.id):
                await self.unauthorized_response(update)
                return
            
            user_id = update.effective_user.id
            
            # Check if in terminal mode
            if user_id in self.script_manager.terminal_sessions:
                command = update.message.text.strip()
                if command:
                    # Send command to interactive terminal
                    success, message = self.script_manager.send_input_to_terminal(user_id, command, add_newline=True)
                    
                    if success:
                        # Wait for output
                        await asyncio.sleep(0.5)
                        output = self.script_manager.read_terminal_output(user_id, timeout=3.0)
                        
                        if output and output != "No output received":
                            # Truncate if too long
                            if len(output) > 4000:
                                output = output[:4000] + "\n\n\\.\\.\\. \\(output truncated\\)"
                            
                            await update.message.reply_text(f"```\n{output}\n```", parse_mode=ParseMode.MARKDOWN_V2)
                        else:
                            # No immediate output, acknowledge command
                            await update.message.reply_text(f"📝 *Command sent:* `{escape_markdown(command)}`", parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        await update.message.reply_text(f"❌ *Terminal error:* `{escape_markdown(message)}`", parse_mode=ParseMode.MARKDOWN_V2)
                        # Terminal might have died, restart it
                        if "session has ended" in message.lower():
                            success, restart_msg = self.script_manager.start_interactive_terminal(user_id)
                            if success:
                                await update.message.reply_text("🔄 Terminal session restarted automatically")
                            else:
                                await update.message.reply_text("❌ Failed to restart terminal session")
                    
        except Exception as e:
            logger.error(f"Error in handle_text: {e}")
            try:
                await update.message.reply_text(f"❌ *Error handling message:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    @resilient_api_call
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        try:
            query = update.callback_query
            await query.answer()
            
            if not self.is_admin(query.from_user.id):
                await query.edit_message_text("🚫 **ACCESS DENIED**\n\nUnauthorized access attempt logged.")
                return
            
            data = query.data
            
            if data == "list_scripts":
                await self.list_scripts_callback(query, context)
            elif data == "server_status":
                await self.server_status_callback(query, context)
            elif data == "toggle_terminal":
                await self.toggle_terminal_callback(query, context)
            elif data == "list_processes":
                await self.list_processes_callback(query, context)
            elif data == "export_backup":
                await self.export_backup_callback(query, context)
            elif data == "backup_menu":
                await self.backup_menu_callback(query, context)
            elif data.startswith("manage_"):
                script_id = data.split("_", 1)[1]
                await self.script_management_menu(query, script_id)
            elif data.startswith("start_"):
                script_id = data.split("_", 1)[1]
                await self.start_script_callback(query, script_id)
            elif data.startswith("stop_"):
                script_id = data.split("_", 1)[1]
                await self.stop_script_callback(query, script_id)
            elif data.startswith("restart_"):
                script_id = data.split("_", 1)[1]
                await self.restart_script_callback(query, script_id)
            elif data.startswith("logs_"):
                script_id = data.split("_", 1)[1]
                await self.show_logs_callback(query, script_id)
            elif data.startswith("toggle_auto_"):
                script_id = data.split("_", 2)[2]
                await self.toggle_auto_restart_callback(query, script_id)
            elif data.startswith("delete_"):
                script_id = data.split("_", 1)[1]
                await self.delete_script_callback(query, script_id)
            elif data == "upload_help":
                await self.upload_help_callback(query, context)
            elif data == "main_menu":
                await self.main_menu_callback(query, context)
                
        except Exception as e:
            logger.error(f"Error in button callback: {e}")

    async def upload_help_callback(self, query, context):
        """Upload help callback"""
        try:
            help_text = """
📤 *How to Upload Scripts*

1\\. *Supported File Types:*
   • `\\.py` \\- Python scripts
   • `\\.sh` \\- Shell/Bash scripts
   • `\\.js` \\- JavaScript/Node\\.js scripts

2\\. *Upload Process:*
   • Send file as attachment
   • Bot will auto\\-detect script type
   • Get instant management buttons
   • Start/stop with one click

3\\. *Enhanced Features:*
   • Auto\\-restart on crash
   • Real\\-time logs
   • Background execution
   • *Script\\-specific input support*
   • Process monitoring

4\\. *Interactive Script Support:*
   • Upload interactive scripts
   • Use `/sinput <script_id> <input>` for passwords/prompts
   • Multiple scripts can run simultaneously
   • Independent from global terminal mode

📎 *Ready to upload?* Just send your script file\\!
            """
            
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="list_scripts")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in upload_help_callback: {e}")
            try:
                await query.edit_message_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def main_menu_callback(self, query, context):
        """Main menu callback"""
        try:
            welcome_text = """
🤖 *Enhanced Advanced Hosting Management Bot*

Your enhanced server management dashboard:

🔧 *Quick Actions:*
• Manage your scripts with input support
• Check server status  
• Access interactive terminal mode
• Monitor processes

🎯 *Features:*
• Script\\-specific input commands
• Enhanced Start button reliability
• Multi\\-script interaction support

🔒 *Security:* Admin\\-only access active

Choose an option below to get started:
            """
            
            keyboard = [
                [InlineKeyboardButton("📂 My Scripts", callback_data="list_scripts")],
                [InlineKeyboardButton("📊 Server Status", callback_data="server_status")],
                [InlineKeyboardButton("🖥️ Terminal Mode", callback_data="toggle_terminal")],
                [InlineKeyboardButton("📦 Backup Menu", callback_data="backup_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in main_menu_callback: {e}")
            try:
                await query.edit_message_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def backup_menu_callback(self, query, context):
        """Show backup management menu"""
        try:
            menu_text = """📦 *Backup Management*

🔧 *Available Options:*
• Export current bot data to a local backup file\\.
• Use `/importlink <url>` to restore from a backup\\.

⚠️ *Important Notes:*
• Restoring from a backup will replace ALL current data\\.
• A backup of the current state is created before restoration\\.

Choose an option below:"""
            
            keyboard = [
                [InlineKeyboardButton("📤 Export Backup", callback_data="export_backup")],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in backup_menu_callback: {e}")
            try:
                await query.edit_message_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def script_management_menu(self, query, script_id: str):
        """Show script management menu - ENHANCED"""
        try:
            scripts = self.script_manager.list_scripts()
            script = next((s for s in scripts if s['id'] == script_id), None)
            
            if not script:
                await query.edit_message_text("❌ Script not found")
                return
            
            status_emoji = "🟢 Running" if script['status'] == 'running' else "🔴 Stopped"
            auto_restart_status = "🔄 Enabled" if script.get('auto_restart', False) else "❌ Disabled"
            input_ready = "🎯 Ready" if script_id in self.script_manager.script_stdin_pipes else "❌ Not Available"
            
            text = f"""
*⚙️ Enhanced Script Management*

📄 *Name:* `{escape_markdown(script['original_name'])}`
🆔 *ID:* `{script['id']}`
🔧 *Type:* `{escape_markdown(script['script_type'])}`
📊 *Status:* {escape_markdown(status_emoji)}
🔄 *Auto\\-restart:* {escape_markdown(auto_restart_status)}
🎯 *Input Ready:* {escape_markdown(input_ready)}
📈 *Restarts:* `{script.get('restart_count', 0)}`
            """
            
            if script.get('last_started'):
                text += f"\n🕐 *Last Started:* `{script['last_started'][:19]}`"
            
            if script.get('pid'):
                text += f"\n🔢 *PID:* `{script['pid']}`"
            
            # Enhanced input instructions
            if script_id in self.script_manager.script_stdin_pipes:
                text += f"\n\n💡 *Send Input:*\n• `/sinput {script_id} <text>`\n• `/pinput {script.get('pid', 'N/A')} <text>`"
            
            keyboard = []
            
            if script['status'] == 'running':
                keyboard.append([InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_{script_id}")])
                keyboard.append([InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{script_id}")])
            else:
                keyboard.append([InlineKeyboardButton("▶️ Start", callback_data=f"start_{script_id}")])
            
            keyboard.append([InlineKeyboardButton("📋 View Logs", callback_data=f"logs_{script_id}")])
            
            auto_text = "Disable Auto\\-restart" if script.get('auto_restart', False) else "Enable Auto\\-restart"
            keyboard.append([InlineKeyboardButton(f"🔄 {auto_text}", callback_data=f"toggle_auto_{script_id}")])
            
            keyboard.append([InlineKeyboardButton("🗑️ Delete Script", callback_data=f"delete_{script_id}")])
            keyboard.append([InlineKeyboardButton("🔙 Back to Scripts", callback_data="list_scripts")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in script_management_menu: {e}")
            try:
                await query.edit_message_text(f"❌ Error: {str(e)}")
            except:
                pass

    async def start_script_callback(self, query, script_id: str):
        """Start script callback - ENHANCED to fix Start button issues"""
        try:
            # Show immediate feedback
            await query.edit_message_text("🚀 Starting script...")
            
            # Validate script exists before attempting to start
            if script_id not in self.script_manager.scripts:
                await query.edit_message_text("❌ *Script not found*\\. Please refresh and try again\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            script_info = self.script_manager.scripts[script_id]
            if not os.path.exists(script_info['file_path']):
                await query.edit_message_text(f"❌ *Script file missing:* `{escape_markdown(script_info['file_path'])}`", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            # Attempt to start the script
            success, message = self.script_manager.start_script(script_id)
            status_emoji = "✅" if success else "❌"
            
            # Show result message
            result_text = f"{status_emoji} {escape_markdown(message)}"
            if success:
                result_text += f"\n\n🎯 *Input ready:* `/sinput {script_id} <text>`"
            
            await query.edit_message_text(result_text, parse_mode=ParseMode.MARKDOWN_V2)
            
            # Show management menu after 3 seconds
            await asyncio.sleep(3)
            await self.script_management_menu(query, script_id)
            
        except Exception as e:
            logger.error(f"Error in start_script_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Start failed:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
                # Still try to show management menu after error
                await asyncio.sleep(2)
                await self.script_management_menu(query, script_id)
            except:
                pass

    async def stop_script_callback(self, query, script_id: str):
        """Stop script callback"""
        try:
            await query.edit_message_text("⏹️ Stopping script...")
            
            success, message = self.script_manager.stop_script(script_id)
            status_emoji = "✅" if success else "❌"
            
            await query.edit_message_text(f"{status_emoji} {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
            
            # Show management menu after 2 seconds
            await asyncio.sleep(2)
            await self.script_management_menu(query, script_id)
            
        except Exception as e:
            logger.error(f"Error in stop_script_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def restart_script_callback(self, query, script_id: str):
        """Restart script callback"""
        try:
            await query.edit_message_text("🔄 Restarting script...")
            
            success, message = self.script_manager.restart_script(script_id)
            status_emoji = "✅" if success else "❌"
            
            result_text = f"{status_emoji} {escape_markdown(message)}"
            if success:
                result_text += f"\n\n🎯 *Input ready:* `/sinput {script_id} <text>`"
            
            await query.edit_message_text(result_text, parse_mode=ParseMode.MARKDOWN_V2)
            
            # Show management menu after 3 seconds
            await asyncio.sleep(3)
            await self.script_management_menu(query, script_id)
            
        except Exception as e:
            logger.error(f"Error in restart_script_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def show_logs_callback(self, query, script_id: str):
        """Show script logs"""
        try:
            logs = self.script_manager.get_script_logs(script_id)
            
            if len(logs) > 4000:
                logs = logs[-4000:] + "\n\n\\.\\.\\. \\(truncated\\)"
            
            script = next((s for s in self.script_manager.list_scripts() if s['id'] == script_id), None)
            script_name = script['original_name'] if script else script_id
            
            text = f"📋 *Logs for* `{escape_markdown(script_name)}`\n\n```\n{logs}\n```"
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Logs", callback_data=f"logs_{script_id}")],
                [InlineKeyboardButton("🔙 Back to Management", callback_data=f"manage_{script_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in show_logs_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def toggle_auto_restart_callback(self, query, script_id: str):
        """Toggle auto-restart callback"""
        try:
            success, message = self.script_manager.toggle_auto_restart(script_id)
            status_emoji = "✅" if success else "❌"
            
            await query.edit_message_text(f"{status_emoji} {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
            
            # Show management menu after 1 second
            await asyncio.sleep(1)
            await self.script_management_menu(query, script_id)
            
        except Exception as e:
            logger.error(f"Error in toggle_auto_restart_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def delete_script_callback(self, query, script_id: str):
        """Delete script callback"""
        try:
            await query.edit_message_text("🗑️ Deleting script...")
            
            success, message = self.script_manager.delete_script(script_id)
            status_emoji = "✅" if success else "❌"
            
            await query.edit_message_text(f"{status_emoji} {escape_markdown(message)}", parse_mode=ParseMode.MARKDOWN_V2)
            
            # Go back to scripts list after 2 seconds
            await asyncio.sleep(2)
            await self.list_scripts_callback(query, None)
            
        except Exception as e:
            logger.error(f"Error in delete_script_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def list_scripts_callback(self, query, context):
        """List scripts callback"""
        try:
            scripts = self.script_manager.list_scripts()
            
            if not scripts:
                text = "📂 *No scripts found*\n\nUpload a `\\.py`, `\\.sh`, or `\\.js` file to get started\\!"
                keyboard = [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
            else:
                text = "📂 *Your Enhanced Scripts:*\n\n"
                keyboard = []
                
                for script in sorted(scripts, key=lambda x: x['created_at'], reverse=True):
                    status_emoji = "🟢" if script['status'] == 'running' else "🔴"
                    auto_restart_emoji = "🔄" if script.get('auto_restart', False) else ""
                    input_ready_emoji = "🎯" if script['id'] in self.script_manager.script_stdin_pipes else ""
                    
                    text += f"{status_emoji} *{escape_markdown(script['original_name'])}* {auto_restart_emoji}{input_ready_emoji}\n"
                    text += f"   • *Status:* {escape_markdown(script['status'])}\n"
                    text += f"   • *Type:* `{escape_markdown(script['script_type'])}`\n"
                    if input_ready_emoji:
                        text += f"   • *Input:* `/sinput {script['id']} <text>`\n"
                    text += "\n"
                    
                    keyboard.append([
                        InlineKeyboardButton(f"⚙️ {script['original_name'][:15]}", 
                                           callback_data=f"manage_{script['id']}")
                    ])
                
                text += "🎯 \\= Input Ready \\| 🔄 \\= Auto\\-restart \\| 🟢 \\= Running\n"
                keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="list_scripts")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in list_scripts_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def server_status_callback(self, query, context):
        """Server status callback"""
        try:
            status_parts = []
            
            # System metrics with error handling
            try:
                cpu_percent = psutil.cpu_percent(interval=1)
                status_parts.append(f"• *CPU:* {escape_markdown(f'{cpu_percent}%')}")
            except Exception:
                status_parts.append("• *CPU:* Unable to read")
            
            try:
                memory = psutil.virtual_memory()
                status_parts.append(f"• *Memory:* {escape_markdown(f'{memory.percent}%')} \\({escape_markdown(f'{memory.used // (1024**3)}GB / {memory.total // (1024**3)}GB')}\\)")
            except Exception:
                status_parts.append("• *Memory:* Unable to read")
            
            try:
                disk = psutil.disk_usage('/')
                status_parts.append(f"• *Disk:* {escape_markdown(f'{disk.percent}%')} \\({escape_markdown(f'{disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB')}\\)")
            except Exception:
                status_parts.append("• *Disk:* Unable to read")
                
            try:
                running_scripts = len([s for s in self.script_manager.list_scripts() if s['status'] == 'running'])
                total_scripts = len(self.script_manager.scripts)
                scripts_with_input = len(self.script_manager.script_stdin_pipes)
            except Exception:
                running_scripts = 0
                total_scripts = 0
                scripts_with_input = 0
            
            active_terminals = len(self.script_manager.interactive_processes)
            
            status_text = f"""*📊 Enhanced Server Status*

*⚡ Performance:*
{chr(10).join(status_parts)}

*🔄 Scripts Status:*
• *Running:* {running_scripts}/{total_scripts}
• *Interactive Ready:* {scripts_with_input}
• *Total Managed:* {total_scripts}

*🖥️ Terminal Sessions:*
• *Active Interactive:* {active_terminals}

*🔋 Health:* 🟢 Enhanced & Operational"""
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="server_status")],
                [InlineKeyboardButton("📂 Scripts", callback_data="list_scripts")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(status_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in server_status_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def toggle_terminal_callback(self, query, context):
        """Toggle terminal callback"""
        try:
            user_id = query.from_user.id
            
            if user_id in self.script_manager.terminal_sessions:
                # Stop interactive terminal
                self.script_manager.stop_interactive_terminal(user_id)
                del self.script_manager.terminal_sessions[user_id]
                
                await query.edit_message_text(
                    "🖥️ *Interactive Terminal Disabled*\n\n"
                    "✅ Terminal session ended\n"
                    "🔙 Back to normal bot mode\n\n"
                    "💡 *Script input commands still available:*\n"
                    "• `/sinput <script_id> <text>`\n"
                    "• `/pinput <pid> <text>`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                # Start interactive terminal
                success, message = self.script_manager.start_interactive_terminal(user_id)
                
                if success:
                    self.script_manager.terminal_sessions[user_id] = {
                        'enabled': True,
                        'started_at': datetime.now().isoformat()
                    }
                    
                    await query.edit_message_text(
                        "🖥️ *Interactive Terminal Enabled*\n\n"
                        "✅ Terminal session started\n"
                        "📝 Every message \\= shell command\n"
                        "⌨️ *Input Commands Available:*\n"
                        "• `/enter <text>` \\- Send input \\+ Enter\n"
                        "• `/space` \\- Send space key\n"
                        "• `/ctrl_c` \\- Send Ctrl\\+C\n"
                        "• `/input <text>` \\- Send raw input\n\n"
                        "🎯 *Script Input Still Works:*\n"
                        "• `/sinput <script_id> <text>`\n"
                        "• `/pinput <pid> <text>`\n\n"
                        "🚨 *Enhanced:* No more freezing issues\\!\n"
                        "Type `/terminal` again to disable\\."
                    , parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await query.edit_message_text(f"❌ *Failed to start terminal:* `{escape_markdown(message)}`", parse_mode=ParseMode.MARKDOWN_V2)
                
        except Exception as e:
            logger.error(f"Error in toggle_terminal_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def list_processes_callback(self, query, context):
        """List processes callback"""
        try:
            processes = []
            try:
                for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                    try:
                        proc_info = proc.info
                        if proc_info['cpu_percent'] > 0 or proc_info['memory_percent'] > 0.1:
                            processes.append(proc_info)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
            except Exception as e:
                await query.edit_message_text(f"❌ *Error accessing process list:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            # Sort by CPU usage
            processes.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)
            processes = processes[:20]  # Top 20
            
            if not processes:
                text = "🔄 *No active processes found*\n\nThis may be due to system permission restrictions\\."
            else:
                text = "🔄 *Top Running Processes:*\n\n"
                for proc in processes:
                    cpu = proc.get('cpu_percent', 0)
                    mem = proc.get('memory_percent', 0)
                    name = proc.get('name', 'Unknown')
                    pid = proc.get('pid', 'Unknown')
                    text += f"• *PID* `{pid}`: `{escape_markdown(name)}`\n"
                    text += f"  *CPU:* {escape_markdown(f'{cpu:.1f}%')} \\| *RAM:* {escape_markdown(f'{mem:.1f}%')}\n\n"
            
            keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="list_processes")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            
        except Exception as e:
            logger.error(f"Error in list_processes_callback: {e}")
            try:
                await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            except:
                pass

    async def export_backup_callback(self, query, context):
        """Export backup callback using asyncio.to_thread for robust background execution."""
        if not self.script_manager.get_dropbox_client():
            await query.edit_message_text(
                "❌ *Dropbox Not Configured*\n"
                "Run `/setup_dropbox` and `/dropbox_code` first\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await query.answer()
            return

        await query.edit_message_text("🔄 Starting backup and upload process...")
        await query.answer()

        chat_id = query.message.chat_id
        message_id = query.message.message_id

        def _blocking_backup_and_upload():
            """Wrapper for the entire blocking backup and upload process."""
            success, message, backup_path = self.script_manager.create_backup(is_automatic=False)
            if not success:
                # Schedule a job to notify of failure
                context.application.job_queue.run_once(
                    _edit_admin_notification, 0,
                    data={"chat_id": chat_id, "message_id": message_id, "text": f"❌ *Backup creation failed:* `{escape_markdown(message)}`"}
                )
                return

            # If backup is successful, proceed to upload
            self.script_manager.upload_to_dropbox(backup_path, True, chat_id, message_id)

        # Run the entire blocking operation in a separate thread
        try:
            await asyncio.to_thread(_blocking_backup_and_upload)
        except Exception as e:
            logger.error(f"Error in export_backup_callback thread: {e}")
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ *An unexpected error occurred during the backup process:* `{escape_markdown(str(e))}`",
                parse_mode=ParseMode.MARKDOWN_V2
            )


    @resilient_api_call
    async def setup_dropbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command to set up Dropbox OAuth credentials."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        if len(context.args) != 2:
            await update.message.reply_text(
                "❌ *Usage:* `/setup_dropbox <APP_KEY> <APP_SECRET>`",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        app_key, app_secret = context.args
        auth_flow = dropbox.DropboxOAuth2FlowNoRedirect(
            app_key, app_secret, token_access_type='offline'
        )
        auth_url = auth_flow.start()

        self.script_manager.dropbox_config.update({
            "app_key": app_key,
            "app_secret": app_secret,
            "refresh_token": None,
            "access_token": None,
            "expires_at": None,
        })
        self.script_manager.save_dropbox_config()

        message = (
            "✅ *Dropbox Setup Initiated*\n\n"
            "1\\. *Authorize the bot* by visiting this URL:\n"
            f"   [Dropbox Authorization Link]({auth_url})\n\n"
            "2\\. *Grant access* and copy the authorization code provided\\.\n\n"
            "3\\. *Send the code* back to the bot using the command:\n"
            "   `/dropbox_code <YOUR_CODE>`"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def dropbox_code_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command to handle the Dropbox authorization code."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        if not context.args:
            await update.message.reply_text("❌ *Usage:* `/dropbox_code <AUTHORIZATION_CODE>`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        auth_code = context.args[0]
        config = self.script_manager.dropbox_config
        app_key = config.get("app_key")
        app_secret = config.get("app_secret")

        if not app_key or not app_secret:
            await update.message.reply_text("❌ *App key/secret not found\\.* Please run `/setup_dropbox` first\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        try:
            auth_flow = dropbox.DropboxOAuth2FlowNoRedirect(app_key, app_secret, token_access_type='offline')
            oauth_result = auth_flow.finish(auth_code)

            config.update({
                "access_token": oauth_result.access_token,
                "refresh_token": oauth_result.refresh_token,
                # Dropbox access tokens expire in 4 hours (14400 seconds).
                "expires_at": time.time() + 14400,
            })
            self.script_manager.save_dropbox_config()

            await update.message.reply_text("✅ *Dropbox authentication successful\\!*\\nThe bot is now authorized to upload backups\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Dropbox code exchange failed: {e}")
            await update.message.reply_text(f"❌ *Authentication Failed:*\n`{escape_markdown(str(e))}`\n"
                                          f"Please try the setup process again\\.", parse_mode=ParseMode.MARKDOWN_V2)

    @resilient_api_call
    async def dropbox_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays the status of the Dropbox integration."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        status_message = self.script_manager.get_dropbox_status()
        await update.message.reply_text(status_message, parse_mode=ParseMode.MARKDOWN_V2)

    async def refresh_dropbox_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin-only command to manually refresh Dropbox token."""
        if not self.is_admin(update.effective_user.id):
            await self.unauthorized_response(update)
            return

        success, message = self.script_manager.refresh_dropbox_token()
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)

    def run(self):
        """Run the bot with automatic restart on network errors."""
        self.application.add_error_handler(self.error_handler)

        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.server_status))
        self.application.add_handler(CommandHandler("scripts", self.list_scripts))
        self.application.add_handler(CommandHandler("cmd", self.execute_command))
        self.application.add_handler(CommandHandler("ps", self.list_processes))
        self.application.add_handler(CommandHandler("kill", self.kill_process))
        self.application.add_handler(CommandHandler("sinput", self.send_script_input))
        self.application.add_handler(CommandHandler("pinput", self.send_pid_input))
        self.application.add_handler(CommandHandler("enter", self.send_enter_input))
        self.application.add_handler(CommandHandler("space", self.send_space))
        self.application.add_handler(CommandHandler("ctrl_c", self.send_ctrl_c))
        self.application.add_handler(CommandHandler("input", self.send_raw_input))
        self.application.add_handler(CommandHandler("terminal", self.toggle_terminal))
        self.application.add_handler(CommandHandler("export", self.export_backup))
        self.application.add_handler(CommandHandler("importlink", self.import_from_link))
        self.application.add_handler(CommandHandler("test", self.test_command))
        self.application.add_handler(CommandHandler("setup_dropbox", self.setup_dropbox))
        self.application.add_handler(CommandHandler("dropbox_code", self.dropbox_code_handler))
        self.application.add_handler(CommandHandler("dropbox_status", self.dropbox_status))
        self.application.add_handler(CommandHandler("refresh_token", self.refresh_dropbox_token))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))

        logger.info("🚀 Enhanced Advanced Hosting Bot Started!")

        while True:
            try:
                # Run the bot
                self.application.run_polling(drop_pending_updates=True)
            except telegram.error.NetworkError as e:
                logger.error(f"🔌 Network error encountered: {e}. Attempting to reconnect in 5 seconds...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"❌ An unexpected error occurred in the main loop: {e}. Restarting in 15 seconds...")
                time.sleep(15)

def main():
    """Main function"""
    try:
        # Handle shutdown gracefully
        def signal_handler(signum, frame):
            logger.info("🛑 Received shutdown signal")
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start the bot
        bot = TelegramBot()
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")

if __name__ == "__main__":
    main()