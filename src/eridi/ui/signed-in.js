// After a one-time sign-in link: go to the app, keeping any #/page in the link.
"use strict";
location.replace("/" + location.hash);
