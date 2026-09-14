
## 安装

```bash
sudo apt install zsh
```

第一步 → 把 oh-my-zsh 项目 Clone 下来:

```bash
git clone https://github.com/robbyrussell/oh-my-zsh.git ~/.oh-my-zsh
```

第二步 → 复制 .zshrc

```bash
cp ~/.oh-my-zsh/templates/zshrc.zsh-template ~/.zshrc
```

第三步 → 更改你的默认 Shell

```bash
chsh -s /bin/zsh
```

重启terminal,看到修改

## 修改配置

需要修改 `~/.zshrc`

### 历史补全

```bash
git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions.git ${ZSH_CUSTOM:-${ZSH:-~/.oh-my-zsh}/custom}/plugins/zsh-autosuggestions
```

```bash
vim ~/.zshrc
# 找到下面这一行,添加 zsh-autosuggestions
plugins=(git zsh-autosuggestions)
source ~/.zshrc
```

![20230228180507](https://raw.githubusercontent.com/learner-lu/picbed/master/20230228180507.png)

### 实时自动补全

```bash
mkdir $ZSH_CUSTOM/plugins/incr
curl -fsSL https://mimosa-pudica.net/src/incr-0.2.zsh -o $ZSH_CUSTOM/plugins/incr/incr.zsh
echo 'source $ZSH_CUSTOM/plugins/incr/incr.zsh' >> ~/.zshrc
source ~/.zshrc
```

> 开启后会有一点卡

### 语法高亮插件

```bash
git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting
```

```bash
vim ~/.zshrc
# 添加 zsh-syntax-highlighting
plugins=(git zsh-autosuggestions zsh-syntax-highlighting)
```

### conda

把如下内容复制到 .zshrc 结尾

```bash
__conda_setup="$('/home/kamilu/anaconda3/bin/conda' 'shell.bash' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "/home/kamilu/anaconda3/etc/profile.d/conda.sh" ]; then
        . "/home/kamilu/anaconda3/etc/profile.d/conda.sh"
    else
        export PATH="/home/kamilu/anaconda3/bin:$PATH"
    fi
fi
unset __conda_setup
```

替换这里的 `kamilu` 为你的名字就可以了

```bash
:%s/kamilu/myname
```

## 参考

- [ohmyzsh](https://ohmyz.sh/)
- [theme](https://github.com/ohmyzsh/ohmyzsh/wiki/themes)
- [zsh + oh-my-zsh](https://zhuanlan.zhihu.com/p/58073103)
- [Linux Zsh 使用 oh-my-zsh 打造高效便捷的 shell 环境](https://sysin.org/blog/linux-zsh/)