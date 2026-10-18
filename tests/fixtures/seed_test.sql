INSERT INTO companies VALUES ('NW','Northwind',CURRENT_TIMESTAMP);
INSERT INTO org_units (id,company_id,kind,name,parent_id,dept_id) VALUES
 ('TECH','NW','department','Tech',NULL,'TECH'),('FIN','NW','department','Finance',NULL,'FIN'),
 ('AI','NW','team','AI','TECH','TECH'),('DATA','NW','team','Data','TECH','TECH'),
 ('AI-NLP','NW','team','AI NLP','AI','TECH');
INSERT INTO org_unit_closure VALUES
 ('TECH','TECH',0),('FIN','FIN',0),('AI','AI',0),('DATA','DATA',0),('AI-NLP','AI-NLP',0),
 ('TECH','AI',1),('TECH','DATA',1),('TECH','AI-NLP',2),('AI','AI-NLP',1);
INSERT INTO people (id,company_id,name,status) VALUES
 ('nikhil','NW','Nikhil','active'),('dana','NW','Dana (Data)','active'),
 ('arjun','NW','Arjun (Tech head)','active'),('fiona','NW','Fiona (Finance)','active'),('nlp1','NW','NLP eng','active');
INSERT INTO memberships (id,person_id,unit_id,is_leader,valid_from) VALUES
 ('m1','nikhil','AI',FALSE,'2026-01-01'),('m2','dana','DATA',FALSE,'2026-01-01'),
 ('m3','arjun','TECH',TRUE,'2025-01-01'),('m4','fiona','FIN',FALSE,'2025-01-01'),('m5','nlp1','AI-NLP',FALSE,'2026-01-01');
INSERT INTO groups VALUES ('grp-finance','NW','Finance',NULL);
INSERT INTO group_members VALUES ('grp-finance','fiona','2025-01-01',NULL);
INSERT INTO records (id,company_id,source,source_id,record_type,owner_unit_id,dept_id,classification,share_scope,owner_person_id,personal,payload,hash) VALUES
 ('r-pub','NW','policies','p1','policy','TECH','TECH','public',NULL,NULL,FALSE,'{}','h1'),
 ('r-int','NW','policies','p2','policy','TECH','TECH','internal',NULL,NULL,FALSE,'{}','h2'),
 ('r-ai','NW','eng','d1','design','AI','TECH','confidential','team',NULL,FALSE,'{}','h3'),
 ('r-data','NW','eng','d2','pipeline','DATA','TECH','confidential','team',NULL,FALSE,'{}','h4'),
 ('r-budget','NW','fin','b1','budget','TECH','TECH','confidential','leadership',NULL,FALSE,'{}','h5'),
 ('r-rate','NW','fin','rc','ratecard','FIN','FIN','highly_confidential',NULL,NULL,FALSE,'{}','h6'),
 ('r-dm','NW','slack','dm1','message',NULL,NULL,'highly_confidential',NULL,'nikhil',TRUE,'{}','h7');
INSERT INTO grants (id,company_id,subject_type,subject_id,resource_type,resource_id,valid_from) VALUES
 ('g1','NW','group','grp-finance','record','r-rate','2026-01-01');
