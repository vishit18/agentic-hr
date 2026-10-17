# Cleanup confirmation (draft, not sent)

Send this yourself after you have run `scripts\cleanup.ps1 -Confirm` and checked the list at the bottom. It is a draft only. Change anything that is not true before sending.

**To:** isa@usecadence.tech
**Subject:** Agentic HR hub: data deleted after the meeting

Hi Isa,

Thank you for the meeting. As the challenge rules ask, I have deleted every copy of the data from the build:

- The database, its volume and all indexes (including embeddings)
- All CV files in the inbox and request attachments
- Database backups, extracted text and logs
- My hand labels and evaluation reports
- Built containers and images
- The local copy of the cvs folder
- No cloud resources were created. Nothing was deployed to AWS.

Emails from the hub only ever went to a local mail catcher on my machine. Nobody in the CVs was contacted. CV content was only processed by models running locally on my own laptop, and was not sent to any outside service.

Please tell me if you need anything else.

Kind regards,
[your name]

---

## Check before sending (delete this part from the email)
- [ ] `scripts\cleanup.ps1 -Confirm` finished with "Done" and no "Still present" warning
- [ ] The cvs folder is gone (ran with `-IncludeCvFolder`, or you deleted it by hand)
- [ ] Other clones or zip copies of the repo or cvs folder, and anything in Downloads or the recycle bin, are gone
- [ ] Any AI assistant chat or tool history that contained CV content is deleted. The assistant tooling used in this build was told never to open CV files, but check your own history anyway
- [ ] Every line above is true. If any of it is not, change the email rather than the facts
