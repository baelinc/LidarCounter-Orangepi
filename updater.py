import subprocess
import os

def check_for_updates():
    try:
        os.chdir('/home/admin/ShowMonLidarCounter')
        subprocess.run(['git', 'fetch'], check=True)
        
        local_hash = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
        remote_hash = subprocess.check_output(['git', 'rev-parse', 'origin/main']).decode().strip()
        
        if local_hash != remote_hash:
            print("Changes detected. Backing up config.json...")
            # Copy your local settings to a temporary safe spot
            subprocess.run(['cp', 'config.json', '/tmp/config.json.bak'], check=False)

            print("Updating code from GitHub...")
            subprocess.run(['git', 'reset', '--hard', 'origin/main'], check=True)

            print("Restoring your settings...")
            # Move your local settings back over the GitHub default
            subprocess.run(['cp', '/tmp/config.json.bak', 'config.json'], check=False)
            
            print("Restarting services...")
            subprocess.run(['sudo', 'systemctl', 'restart', 'ShowMonLidarCounter.service'], check=False)
            subprocess.run(['sudo', 'systemctl', 'restart', 'LidarCounter.service'], check=False)
            print("Update successful!")
        else:
            print("Already up to date.")
            
    except Exception as e:
        print(f"Update failed: {e}")

if __name__ == "__main__":
    check_for_updates()
