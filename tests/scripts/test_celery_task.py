#!/usr/bin/env python
"""
Test script to manually trigger the building data import task via Celery.

Usage:
    docker compose exec web python scripts/test_celery_task.py
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.tasks_new import download_and_import_building_data
from celery.result import AsyncResult
import time

def main():
    print("=" * 70)
    print("Testing Celery Task: download_and_import_building_data")
    print("=" * 70)
    
    # Trigger the task asynchronously
    print("\n📤 Sending task to Celery worker...")
    task = download_and_import_building_data.delay()
    
    print(f"✅ Task queued successfully!")
    print(f"   Task ID: {task.id}")
    print(f"   Initial State: {task.state}")
    
    print("\n" + "=" * 70)
    print("Monitoring task progress (Ctrl+C to stop monitoring)...")
    print("=" * 70)
    
    try:
        last_state = None
        while True:
            result = AsyncResult(task.id)
            if result.state != last_state:
                last_state = result.state
                print(f"\n[{time.strftime('%H:%M:%S')}] State: {result.state}")
                
                if result.state == 'PROGRESS' and result.info:
                    print(f"  Info: {result.info}")
                elif result.state in ['DOWNLOADING', 'EXTRACTING', 'CLEARING', 'IMPORTING']:
                    if result.info:
                        print(f"  Step: {result.info.get('step', 'Unknown')}")
                elif result.state == 'SUCCESS':
                    print(f"\n🎉 Task completed successfully!")
                    print(f"  Buildings imported: {result.result.get('buildings_imported', 0):,}")
                    print(f"  Features imported: {result.result.get('features_imported', 0):,}")
                    if 'building_error' in result.result:
                        print(f"  ⚠️  Building error: {result.result['building_error']}")
                    if 'features_error' in result.result:
                        print(f"  ⚠️  Features error: {result.result['features_error']}")
                    break
                elif result.state == 'FAILURE':
                    print(f"\n❌ Task failed!")
                    print(f"  Error: {result.result}")
                    print(f"  Traceback: {result.traceback}")
                    break
            
            time.sleep(2)
            
    except KeyboardInterrupt:
        print("\n\n" + "=" * 70)
        print("Monitoring stopped. Task is still running in background.")
        print("=" * 70)
        print("\nView logs with:")
        print("  docker compose logs -f worker")
        print("\nCheck status later with:")
        print(f"  docker compose exec web python -c \"")
        print(f"from celery.result import AsyncResult")
        print(f"t = AsyncResult('{task.id}')")
        print(f"print(f'State: {{t.state}}')")
        print(f"print(f'Result: {{t.result}}')\"")
        print("=" * 70)
    
    return task.id

if __name__ == '__main__':
    main()
