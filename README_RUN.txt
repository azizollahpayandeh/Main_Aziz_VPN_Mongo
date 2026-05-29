AzizVPN Bot v3 MongoDB

Local Windows quick start:
1) Create .env from .env.example and fill secrets.
2) Start MongoDB locally or use MongoDB Atlas.
3) Run:
   py -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python main.py

Local Docker MongoDB:
   docker run -d --name azizvpn-mongo -p 27017:27017 -v azizvpn_mongo:/data/db mongo:7

Railway:
- Push project to GitHub.
- Add variables from .env in Railway Variables.
- Use MongoDB Atlas URI in MONGO_URI.
- Start command: python main.py

Admin:
- /admin opens admin panel for ADMIN_ID only.

Important:
- Revoke old Telegram token and 3X-UI API token that were shared in chat.
