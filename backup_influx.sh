#!/bin/bash

# Load .env from same directory as this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "$SCRIPT_DIR/.env"
set +a

# Derived config
BACKUP_DIR="/home/admin/influx_backups"
DATE=$(date +%Y-%m-%d)
BACKUP_NAME="influx_backup_$DATE"

# FTP config (from .env: FTP_HOST, FTP_DIR)

# Create backup
mkdir -p "$BACKUP_DIR/$BACKUP_NAME"
influx backup "$BACKUP_DIR/$BACKUP_NAME" --token "$INFLUX_TOKEN"

# Pack as tar.gz
tar -czf "$BACKUP_DIR/$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "$BACKUP_DIR/$BACKUP_NAME"

# Upload to NAS
curl -s --ftp-create-dirs \
     --netrc-file /home/admin/.ftp_credentials \
     -T "$BACKUP_DIR/$BACKUP_NAME.tar.gz" \
     "ftp://$FTP_HOST$FTP_DIR/$BACKUP_NAME.tar.gz"

# Delete local backups older than 4 weeks
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +28 -delete

echo "Backup $BACKUP_NAME completed: $(date)"