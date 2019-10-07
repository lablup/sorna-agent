import asyncio
from pathlib import Path


async def init_sshd_service():
    Path('/tmp/dropbear').mkdir(parents=True, exist_ok=True)
    auth_path = Path('/home/work/.ssh/authorized_keys')
    if not auth_path.is_file():
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.parent.chmod(0o700)
        proc = await asyncio.create_subprocess_exec(
            *[
                '/opt/kernel/dropbearkey',
                '-t', 'ecdsa',
                '-s', '384',
                '-f', '/tmp/dropbear/id_dropbear',
            ],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"sshd init error: {stderr.decode('utf8')}")
        pub_key = stdout.splitlines()[1]
        auth_path.write_bytes(pub_key)
        auth_path.chmod(0o600)

        # Make the generated private key downloadable by users.
        proc = await asyncio.create_subprocess_exec(
            *[
                '/opt/kernel/dropbearconvert',
                'dropbear', 'openssh',
                '/tmp/dropbear/id_dropbear', '/home/work/id_ecdsa',
            ],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"sshd init error: {stderr.decode('utf8')}")
    else:
        auth_path.parent.chmod(0o700)
        auth_path.chmod(0o600)
    proc = await asyncio.create_subprocess_exec(
        *[
            '/opt/kernel/dropbearkey',
            '-t', 'ecdsa',
            '-s', '384',
            '-f', '/tmp/dropbear/dropbear_ecdsa_host_key',
        ],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"sshd init error: {stderr.decode('utf8')}")


async def prepare_sshd_service(service_info):
    cmdargs = [
        '/opt/kernel/dropbear',
        '-r', '/tmp/dropbear/dropbear_ecdsa_host_key',
        '-F',
        '-p', f"0.0.0.0:{service_info['port']}",
    ]
    env = {}
    return cmdargs, env


async def prepare_ttyd_service(service_info):
    shell_path = '/bin/sh'
    if Path('/bin/bash').exists():
        shell_path = '/bin/bash'
    elif Path('/bin/ash').exists():
        shell_path = '/bin/ash'
    cmdargs, env = ['/opt/backend.ai/bin/ttyd', shell_path], {}
    return cmdargs, env