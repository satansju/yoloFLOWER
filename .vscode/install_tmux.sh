#!/bin/bash

# Script for installing tmux on systems where you don't have root access.
# tmux will be installed in $HOME/local/bin.
# It's assumed that wget and a C/C++ compiler are installed.

# exit on error
set -e

TMUX_VERSION=3.3a

# create our directories
mkdir -p $HOME/tmux_tmp
cd $HOME/tmux_tmp

# download source files for tmux, libevent, and ncurses
wget -O tmux-${TMUX_VERSION}.tar.gz https://github.com/tmux/tmux/releases/download/3.3a/tmux-3.3a.tar.gz
wget https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz
wget https://invisible-island.net/datafiles/release/ncurses.tar.gz

# extract files, configure, and compile

############
# libevent #
############
tar xvzf libevent-2.1.12-stable.tar.gz
cd libevent-2.1.12-stable
./configure --prefix=$HOME/.local --disable-shared
make
make install
cd ..

############
# ncurses  #
############
tar xvzf ncurses.tar.gz
cd ncurses-6.3
./configure --prefix=$HOME/.local
make
make install
cd ..

############
# tmux     #
############
tar xvzf tmux-${TMUX_VERSION}.tar.gz
cd tmux-${TMUX_VERSION}
./configure CFLAGS="-I$HOME/.local/include -I$HOME/.local/include/ncurses" LDFLAGS="-L$HOME/.local/lib -L$HOME/.local/include/ncurses -L$HOME/.local/include"
CPPFLAGS="-I$HOME/.local/include -I$HOME/.local/include/ncurses" LDFLAGS="-static -L$HOME/.local/include -L$HOME/.local/include/ncurses -L$HOME/.local/lib" make
cp tmux $HOME/.local/bin
cd ..

# cleanup
rm -rf $HOME/tmux_tmp

echo "$HOME/.local/bin/tmux is now available. You can optionally add $HOME/.local/bin to your PATH."