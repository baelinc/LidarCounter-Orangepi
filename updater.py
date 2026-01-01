import subprocess
import os
import logging

# Configure logging to match your app.py style
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def check_for_updates():
    # Update this path to your Orange Pi Zero 2 project directory
    PROJECT_DIR = '/root/LidarCounter-Orangepi'
    SERVICE_NAME = 'LidarCounter.service'
    
    try:
        if not os.path.exists(PROJECT_DIR):
            print(f"Error: Directory {PROJECT_DIR} does not exist.")
            return

        os.chdir(PROJECT_DIR)
        
        # 1. Fetch latest data from GitHub
        subprocess.run(['git', 'fetch'], check=True)
        
        # 2. Compare local vs remote
        local_hash = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
        remote_hash = subprocess.check_output(['git', 'rev-parse', 'origin/main']).decode().strip()
        
        if local_hash != remote_hash:
            print("Changes detected. Backing up config.json and schedule.json...")
            
            # Backup both config and schedule so you don't lose show times or settings
            subprocess.run(['cp', 'config.json', '/tmp/config.json.bak'], check=False)
            subprocess.run(['cp', 'schedule.json', '/tmp/schedule.json.bak'], check=False)

            print("Updating code from GitHub...")
            # This force-aligns your local code to match the GitHub repository exactly
            subprocess.run(['git', 'reset', '--hard', 'origin/main'], check=True)

            print("Restoring your local settings...")
            subprocess.run(['cp', '/tmp/config.json.bak', 'config.json'], check=False)
            subprocess.run(['cp', '/tmp/schedule.json.bak', 'schedule.json'], check=False)
            
            # 3. Handle Python Dependencies in case requirements.txt changed
            print("Updating dependencies...")
            subprocess.run(['pip', 'install', '-r', 'requirements.txt'], check=False)
            
            print(f"Restarting {SERVICE_NAME}...")
            # Removed 'sudo' since Orange Pi runs this as root
            subprocess.run(['systemctl', 'restart', SERVICE_NAME], check=False)
            
            print("Update successful!")
        else:
            print("Already up to date.")
            
    except Exception as e:
        print(f"Update failed: {e}")

if __name__ == "__main__":
    check_for_updates()
