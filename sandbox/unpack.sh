#!/bin/bash
mkdir -p src
cd ctf_tarballs

# radare2
mkdir -p ../src/radare2
tar -xf radare2.tar.gz -C ../src/radare2 --strip-components=1

# flatter
mkdir -p ../src/flatter
tar -xf flatter.tar.gz -C ../src/flatter --strip-components=1

# stegseek
mkdir -p ../src/stegseek
tar -xf stegseek.tar.gz -C ../src/stegseek --strip-components=1

# RsaCtfTool
mkdir -p ../src/RsaCtfTool
tar -xf RsaCtfTool.tar.gz -C ../src/RsaCtfTool --strip-components=1

echo "✅ 源码全部解压完成，路径：sandbox/src/"
