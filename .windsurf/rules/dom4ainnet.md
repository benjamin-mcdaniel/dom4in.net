---
trigger: always_on
---

1. this project uses cloudflare services first if possible
2. any backend code needs to be using wrangler unless its the collection code and that will be run on a local machine
3. for the frontend do not use serverside js, you will need to split any action that cant be client side safe into a backend/ module that the cloudflare page calls
4. dom4in.net is a site that tracks content about domain names that are taken and or unique, things like number of 1 2 3 4 5 letter domains that are registered or for sale
5. we may run the collector often or not often, it needs to run on a local machine and have options to talk to multiple dns servers, i may add ip switching later to increase rate
6. currently this project is a frontend, a backend to display data from a database on the frontend page, a refining script to refine data collected by the collector script, and a collector script. 
