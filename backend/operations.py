"""Operational summaries without request text, credentials or private prompts."""
import importlib.metadata
import shutil
from datetime import datetime, timezone
import config
import database as db


async def summary():
    conn=await db.get_db()
    try:
        queue=await conn.execute_fetchall("SELECT kind,status,count(*) AS count,min(created_at) AS oldest FROM jobs WHERE status IN ('queued','running','cancelling') GROUP BY kind,status")
        success=await conn.execute_fetchall("SELECT kind,max(ended_at) AS last_success FROM jobs WHERE status='succeeded' GROUP BY kind")
        delivery=await conn.execute_fetchall('SELECT status,count(*) AS count,sum(attempts) AS attempts FROM notification_deliveries GROUP BY status')
        errors=await conn.execute_fetchall("SELECT kind,status,count(*) AS count FROM jobs WHERE status IN ('failed','interrupted','partial') GROUP BY kind,status")
        db_ready=(await conn.execute_fetchall('SELECT 1'))[0][0]==1
    finally:await conn.close()
    now=datetime.now(timezone.utc)
    queues=[]
    for row in queue:
        item=dict(row)
        try:item['oldest_age_seconds']=max(0,int((now-datetime.fromisoformat(item['oldest'].replace('Z','+00:00')).replace(tzinfo=timezone.utc)).total_seconds()))
        except (ValueError,TypeError):item['oldest_age_seconds']=None
        queues.append(item)
    usage=shutil.disk_usage(config.DATA_DIR)
    dependencies={}
    for name in ('fastapi','uvicorn','httpx','chromadb','mcp','recurring-ical-events','icalendar'):
        try:dependencies[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:dependencies[name]='not installed'
    return {'reachable':True,'ready':db_ready and usage.free>100*1024*1024,
        'recent_success':[dict(r) for r in success],'queues':queues,'notification_delivery':[dict(r) for r in delivery],
        'outcomes_requiring_review':[dict(r) for r in errors], 'storage':{'total_bytes':usage.total,'free_bytes':usage.free,
        'pressure':'critical' if usage.free<100*1024*1024 else 'low' if usage.free<1024*1024*1024 else 'normal'},
        'dependencies':dependencies,'errors':(['Free durable storage before submitting more work'] if usage.free<1024*1024*1024 else [])}
