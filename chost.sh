# 修改成你Windows的局域网IP
WIN_IP="192.168.1.10"

# 全部代理环境变量
export ALL_PROXY=socks5://${WIN_IP}:10808
export HTTP_PROXY=http://${WIN_IP}:10809
export HTTPS_PROXY=http://${WIN_IP}:10809
export all_proxy=socks5://${WIN_IP}:10808
export http_proxy=http://${WIN_IP}:10809
export https_proxy=http://${WIN_IP}:10809

# git代理配置
git config --global http.proxy socks5://${WIN_IP}:10808
git config --global https.proxy socks5://${WIN_IP}:10808
