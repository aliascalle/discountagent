# Discount agent 🛒

A small robot that checks prices once a day and emails you when something gets cheaper.
It runs on GitHub's computers for free (GitHub Actions), so your own computer can stay off.

**What it watches**

| Product | Where |
|---|---|
| Professional Secrets carbon steel pans | Professional Secrets' own shop (shop-ps.se), Prisjakt* |
| Bergs Potter *Copenhagen*, green and yellow, 21 cm and 25 cm | Royal Design, Nordic Nest, bergspotter.com*, Prisjakt* |

\* bergspotter.com and Prisjakt show a "checking your browser" page to robots, so they are usually skipped.

**When you get an email**

- a price is **lower** than at the previous check
- a product goes **on sale** (the shop shows a crossed-out "before" price)
- a new **discount code** appears on a shop's pages (e.g. "Use code BLACK20")
- a new **campaign text** appears (e.g. "Black Week – 30% off")

Each email lists the product, the shop, the old price, the new price and a link.
No changes means no email. The very first run sends one "I'm up and running" email listing everything it found.

---

## Setup, step by step (about 15 minutes)

You only do this once. You need:
- your GitHub account (you already have one, since this repository exists)
- your Gmail account

### Step 1: Turn on 2-Step Verification in Gmail

Google only gives "app passwords" (step 2) to accounts that have 2-Step Verification.

1. Go to <https://myaccount.google.com/security>.
2. Under **"How you sign in to Google"**, click **2-Step Verification** and follow the steps.
   (If it already says *On*, skip ahead.)

### Step 2: Create a Gmail "app password"

An app password is a separate 16-letter password that only lets this robot send email.
You never share your real Gmail password, and you can delete the app password at any time.

1. Go to <https://myaccount.google.com/apppasswords>.
2. Under **App name**, type `Discount agent` and click **Create**.
3. Google shows a 16-letter password like `abcd efgh ijkl mnop`.
   **Copy it now** (you can't see it again). Keep the tab open for step 3.

### Step 3: Save your login as GitHub "secrets"

Secrets are locked boxes inside your repository. The robot can use them,
but nobody can read them, not even you after saving. They never appear in the code.

1. Open your repository on github.com: `https://github.com/aliascalle/discountagent`
2. Click **Settings** (the ⚙️ tab at the top right of the repository, not your profile settings).
3. In the left menu, click **Secrets and variables** → **Actions**.
4. Click the green **New repository secret** button and add the first secret:
   - **Name:** `GMAIL_ADDRESS`
   - **Secret:** your Gmail address, e.g. `yourname@gmail.com`
   - Click **Add secret**.
5. Click **New repository secret** again:
   - **Name:** `GMAIL_APP_PASSWORD`
   - **Secret:** the 16-letter app password from step 2 (spaces are fine)
6. *(Optional)* To get the emails at a different address (for example your Outlook), add one more:
   - **Name:** `EMAIL_TO`
   - **Secret:** the address the emails should go to.
   If you skip this, emails go to your Gmail address.

The names must be spelled exactly like this, in capital letters.

### Step 4: Let the robot save prices

The robot saves the prices into the repository after each run, so it needs permission to write.

1. Still in **Settings**, click **Actions** → **General** in the left menu.
2. Scroll down to **Workflow permissions**.
3. Choose **Read and write permissions** and click **Save**.

### Step 5: Make sure the code is on the default branch

GitHub only runs scheduled jobs from the repository's *default branch*.

1. Go to the main page of the repository (click **<> Code**).
2. Click the branch button at the top left (it shows a branch name).
   - If the code is in a pull request, open the **Pull requests** tab, open it, and click **Merge pull request** → **Confirm merge**.
   - If the branch with the code is the only branch, it is already the default, and you don't need to do anything.
     *(Optional: rename it to `main` under **Settings → General → Default branch** → ✏️ pencil icon.)*

### Step 6: Send a test email

1. Click the **Actions** tab at the top of the repository.
   If GitHub asks, click **"I understand my workflows, go ahead and enable them"**.
2. In the left list, click **Discount agent**.
3. On the right, click **Run workflow** ▾, tick **"Only send a test email"**, and click the green **Run workflow** button.
4. After about 30 seconds a run appears with a yellow dot (running), then a ✅ green check (worked) or ❌ red cross (failed).
5. Check your inbox (and spam folder) for **"Discount agent: test email"**.

❌ Red cross? Click the run, then click **check-prices**, and read the red lines.
`Username and Password not accepted` means a secret is wrong: redo step 2 and step 3
(click the secret's name and choose **Update** to change it).

### Step 7: Do the first real run

1. **Actions** → **Discount agent** → **Run workflow**, this time *without* ticking anything → **Run workflow**.
2. When it finishes, click the run. The page shows a **summary table** of every price it found,
   and any shops it couldn't read.
3. You get one "Discount agent is up and running" email with the same list.
4. A new folder **`data/`** appears in the repository:
   - `data/prices.json`: the latest prices (used for comparing next time)
   - `data/history.csv`: every price from every run (open it in Excel or Google Sheets)

**That's it!** From now on it runs by itself every day at about 07:17 Swedish time.
GitHub sometimes starts scheduled runs a little late; that's normal.

---

## Everyday questions

**How do I see what happened today?**
Click the **Actions** tab, then the latest run. The summary shows today's prices.

**How do I change what it watches, or add a shop?**
Open `config.yaml` on github.com, click the ✏️ pencil icon, edit, and click **Commit changes**.
The file has comments explaining each part. For example, to add a shop, copy one of the
retailer blocks (from `- id:` down to its `pages:` lines) and change the name and search address.

**Some products are missing from the summary.** Shops name things differently
(e.g. "Dark green", "Ø21"). Run the workflow by hand with **"Print every product found"** ticked.
Then open the run → **check-prices** → **Check prices** to see every product name the robot saw,
and adjust the `match:` words in `config.yaml` to fit.

**A shop shows up under "Warnings".** That shop couldn't be read that day (it was down,
it blocks robots, or the page address changed). The other shops still work. Prisjakt in particular
often blocks automated visitors; the robot then just skips it. If you find the exact Prisjakt
product page for an item, paste its address under the Prisjakt `pages:` in `config.yaml`.
That works more often than search.

**How do I pause it?** **Actions** → **Discount agent** → **⋯** (top right) → **Disable workflow**.
Do the same and choose **Enable workflow** to restart.

**Does it cost anything?** No. A run takes about a minute, and GitHub Actions is free
for public repositories and includes 2,000 free minutes a month for private ones.

**Will it stop by itself?** GitHub pauses scheduled jobs in a repository with no activity for 60 days.
The robot saves prices every day, which normally counts as activity. If it ever stops, GitHub emails you,
and you can turn it back on from the **Actions** tab.

**Is my password safe?** It is stored only as an encrypted GitHub secret, and GitHub hides it in the logs.
To revoke it, delete the app password at <https://myaccount.google.com/apppasswords>.

---

## How it works (for the curious)

- `discount_agent.py`: the Python script. For each shop it tries, in order:
  1. the shop's Shopify product feed (`/products.json`), which has exact prices and "before" prices,
  2. the WooCommerce product feed,
  3. reading the web pages and the hidden product data (schema.org JSON-LD) that shops publish for Google.

  It also scans the pages for discount codes and campaign banners,
  then compares everything with `data/prices.json` and emails you the differences.
- `config.yaml`: which products and shops to check.
- `.github/workflows/discount-agent.yml`: tells GitHub to run the script daily and save the results.
- `tests/`: automatic tests. Run them with `python -m unittest discover tests`.

To run it on your own computer (optional):

```bash
pip install -r requirements.txt
```

```bash
python discount_agent.py --dry-run --list-all
```

`--dry-run` prints the email instead of sending it and saves nothing.
