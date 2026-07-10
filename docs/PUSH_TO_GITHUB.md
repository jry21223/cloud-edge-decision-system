# 推送到 GitHub

当前目录已经是一个完整 Git 仓库。由于当前 ChatGPT 的 GitHub 连接只支持操作已有仓库，不提供新建仓库接口，因此需要先在 GitHub 创建一个空仓库，再执行推送。

建议仓库名：`cloud-edge-decision-system`  
建议可见性：比赛期间使用 Private。

## 使用 GitHub CLI

```bash
gh repo create cloud-edge-decision-system \
  --private \
  --source=. \
  --remote=origin \
  --push
```

## 使用网页创建空仓库后推送

```bash
git remote add origin git@github.com:jry21223/cloud-edge-decision-system.git
git branch -M main
git push -u origin main
```

不要勾选 GitHub 的“Initialize this repository with a README”，避免与本地首次提交冲突。
