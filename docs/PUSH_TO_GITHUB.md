# GitHub 仓库与协作

当前正式仓库：`jry21223/cloud-edge-decision-system`，默认分支为 `main`，比赛期间保持 Private。

## 克隆

```bash
git clone git@github.com:jry21223/cloud-edge-decision-system.git
cd cloud-edge-decision-system
```

## 日常协作

```bash
git checkout -b feat/<short-name>
# 修改并测试
git add -A
git commit -m "feat: describe change"
git push -u origin feat/<short-name>
```

随后在 GitHub 发起 Pull Request。不要直接向 `main` 提交尚未通过测试的实验代码。
