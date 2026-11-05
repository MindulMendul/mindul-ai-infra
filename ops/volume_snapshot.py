"""Runs only in a short-lived Python helper with explicitly mounted data paths."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tarfile
import time


def sqlite_file(path):
    try:
        with path.open('rb') as handle:return handle.read(16)==b'SQLite format 3\x00'
    except (OSError,IsADirectoryError):return False


def inspect_sqlite(path):
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=10)
    try:
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok',str(path)
        tables={}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            tables[name]=db.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
        return tables
    finally:db.close()


def snapshot(source,target,exclude):
    target.mkdir(parents=True,exist_ok=True)
    data=target/'data';data.mkdir()
    owners={};databases={};links={};total=0
    budget=int(os.environ.get('MAX_BYTES',str(2*1024**3)))
    for root,dirs,files in os.walk(source):
        relative=Path(root).relative_to(source)
        dirs[:]=[name for name in dirs if str(relative/name) not in exclude]
        destination=data/relative;destination.mkdir(exist_ok=True)
        st=Path(root).stat()
        owners[str(relative)]={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
        for name in list(dirs):
            path=Path(root)/name
            if path.is_symlink():
                rel=str(relative/name);links[rel]=os.readlink(path);st=path.lstat()
                owners[rel]={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
                dirs.remove(name)
        sqlite_names={name for name in files if sqlite_file(Path(root)/name)}
        for name in files:
            src=Path(root)/name;rel=relative/name
            if str(rel) in exclude or any(name==base+suffix for base in sqlite_names for suffix in ('-wal','-shm','-journal')):continue
            dest=data/rel
            if src.is_symlink():
                links[str(rel)]=os.readlink(src);st=src.lstat()
                owners[str(rel)]={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
                continue
            if not src.is_file():continue
            st=src.stat()
            total+=st.st_size
            if total>budget:raise RuntimeError('Volume exceeds remaining backup byte budget')
            if name in sqlite_names:
                reader=sqlite3.connect(src.as_uri()+'?mode=ro',uri=True,timeout=15)
                writer=sqlite3.connect(dest)
                deadline=time.monotonic()+90
                def progress(*_):
                    if time.monotonic()>deadline:raise TimeoutError('SQLite online backup exceeded 90 seconds')
                try:reader.backup(writer,pages=256,progress=progress,sleep=.05)
                finally:writer.close();reader.close()
                databases[str(rel)]=inspect_sqlite(dest)
            else:shutil.copyfile(src,dest)
            os.chmod(dest,stat.S_IMODE(st.st_mode))
            owners[str(rel)]={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
    (target/'owners.json').write_text(json.dumps(owners))
    (target/'sqlite.json').write_text(json.dumps(databases))
    (target/'links.json').write_text(json.dumps(links))
    uid,gid=int(os.environ['OUT_UID']),int(os.environ['OUT_GID'])
    for root,dirs,files in os.walk(target):
        for path in [Path(root),*[Path(root)/name for name in files]]:
            os.chown(path,uid,gid,follow_symlinks=False)
            if not path.is_symlink():os.chmod(path,0o700 if path.is_dir() else 0o600)
    print(json.dumps({'sqlite_files':len(databases),'files':len(owners)}))


def restore(source,target):
    if any(target.iterdir()):raise RuntimeError('Restore target volume must be empty')
    shutil.copytree(source/'data',target,dirs_exist_ok=True,symlinks=True)
    link_file=source/'links.json'
    for relative,value in (json.loads(link_file.read_text()) if link_file.exists() else {}).items():
        path=target/relative;path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(value)
    for relative,owner in json.loads((source/'owners.json').read_text()).items():
        path=target/relative
        os.chown(path,owner['uid'],owner['gid'],follow_symlinks=False)
        if not path.is_symlink():os.chmod(path,owner['mode'])
    expected=json.loads((source/'sqlite.json').read_text())
    for relative,tables in expected.items():
        actual=inspect_sqlite(target/relative)
        assert actual==tables,f'SQLite row counts differ: {relative}'
    print(json.dumps({'verified_sqlite_files':len(expected)}))


def import_tar(target):
    # Docker cp provides the original UID/GID. Validate every path before restoring it.
    with tarfile.open(fileobj=sys.stdin.buffer,mode='r|*') as archive:
        for member in archive:
            safe=tarfile.data_filter(member,str(target))
            if safe is None:continue
            archive.extract(safe,path=target,filter='data')
            path=target/safe.name
            os.chown(path,member.uid,member.gid,follow_symlinks=False)
            if not path.is_symlink():os.chmod(path,stat.S_IMODE(member.mode))


if __name__=='__main__':
    command=sys.argv[1]
    if command=='snapshot':
        try:snapshot(Path('/source'),Path('/snapshot'),set(json.loads(os.environ.get('EXCLUDE','[]'))))
        finally:
            for root,dirs,files in os.walk('/snapshot'):
                for path in [Path(root),*[Path(root)/name for name in files]]:
                    os.chown(path,int(os.environ['OUT_UID']),int(os.environ['OUT_GID']),follow_symlinks=False)
                    if not path.is_symlink():os.chmod(path,0o700 if path.is_dir() else 0o600)
    elif command=='restore':restore(Path('/snapshot'),Path('/target'))
    elif command=='import-tar':import_tar(Path('/target'))
    elif command=='verify':
        expected=json.loads(Path('/snapshot/sqlite.json').read_text())
        for path,tables in expected.items():assert inspect_sqlite(Path('/source')/path)==tables
        print(json.dumps({'verified_sqlite_files':len(expected)}))
