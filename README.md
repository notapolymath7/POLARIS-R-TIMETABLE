# POLARIS R — Auto-Updating Schedule Website

Your timetable site that **automatically fetches the latest PDF** every hour and publishes the schedule online — for free, using GitHub.

---

## 🚀 Setup (one-time, ~10 minutes)

### Step 1 — Create a GitHub account
Go to https://github.com and sign up (free).

### Step 2 — Create a new repository
1. Click **+** → **New repository**
2. Name it anything, e.g. `polaris-schedule`
3. Set it to **Public** (required for free GitHub Pages)
4. Click **Create repository**

### Step 3 — Upload these files
Upload ALL files from this folder to your new repo:
- `index.html`
- `bot.py`
- `schedule_data.js`
- `.github/workflows/update-schedule.yml`

You can drag-and-drop them in the GitHub web UI, or use:
```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/polaris-schedule.git
git add .
git commit -m "initial commit"
git push -u origin main
```

### Step 4 — Enable GitHub Pages
1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select **GitHub Actions**
3. Click **Save**

### Step 5 — Run the bot for the first time
1. Go to your repo → **Actions** tab
2. Click **Update POLARIS R Schedule** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Wait ~2 minutes for it to finish

### Step 6 — View your live site
Your site will be live at:
```
https://YOUR_USERNAME.github.io/polaris-schedule/
```

---

## ⏰ How it works

| What | When |
|------|------|
| Bot fetches PDF from aurousacademy.com | Every hour automatically |
| If PDF changed → parses new schedule | Immediately after fetch |
| Commits `schedule_data.js` to repo | Automatically |
| GitHub Pages deploys updated site | Within 1-2 minutes |

---

## 🔧 Customise the schedule

- **Change check frequency**: Edit the `cron: '0 * * * *'` line in `.github/workflows/update-schedule.yml`
  - Every 30 min: `'*/30 * * * *'`
  - Twice a day: `'0 8,20 * * *'`
- **Run manually**: Go to Actions → Update POLARIS R Schedule → Run workflow

---

## ❓ Troubleshooting

- **Site shows "Loading..."**: The bot hasn't run yet. Trigger it manually from Actions tab.
- **Schedule not updating**: Check the Actions tab for error logs.
- **Bot fails**: The old schedule stays live — no crash. Check if aurousacademy.com is up.
