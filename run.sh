#!/bin/bash

apt-get update
apt-get install openssh-server -y

mkdir -p /run/sshd
chmod 755 /run/sshd

/usr/sbin/sshd

pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python app.py