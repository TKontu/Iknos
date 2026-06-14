# 🧠 Git Cheat Sheet

git init # init new repo
git clone <url> # clone repo
git remote add origin <url> # add remote
git remote -v # show remotes

git status # check status
git add <file> # stage file
git add . # stage all
git commit -m "msg" # commit changes

git branch # list local branches
git branch -a # list all branches
git branch <name> # create branch
git checkout <name> # switch branch
git checkout -b <name> # create + switch

git push origin <branch> # push branch
git push -u origin <branch> # push + set upstream
git pull # pull latest
git pull origin <branch> # pull specific branch
git fetch # fetch only

git merge <branch> # merge branch
git rebase <branch> # rebase onto branch

git log --oneline --graph --all # compact commit history
git diff # show changes
git checkout -- <file> # discard changes
git reset <file> # unstage file
git reset # unstage all
git branch -d <name> # delete branch (safe)
git branch -D <name> # delete branch (force)

git branch -m <new-name> # rename branch
git rm -r --cached <path> # remove cached files
git rm -r --cached .

git remote rm origin