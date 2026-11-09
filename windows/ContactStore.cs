namespace GaevAssistant;
static class ContactStore {
 public static void Import(){
  Data.Execute("CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,name TEXT,person TEXT,phones TEXT,email TEXT,category TEXT,source TEXT,description TEXT,called INTEGER DEFAULT 0,note TEXT DEFAULT '')");
  if(Data.Table("assistant.sqlite3","SELECT name FROM pragma_table_info('contacts') WHERE name='interested'").Rows.Count==0)
   Data.Execute("ALTER TABLE contacts ADD COLUMN interested INTEGER NOT NULL DEFAULT 0");
  if(!File.Exists(Data.FileAt("contacts.html")))return;
  var cards=ContactCard.Read(File.ReadAllText(Data.FileAt("contacts.html")));
  var names=File.Exists(Data.FileAt("outreach-links.sqlite3"))?Data.Table("outreach-links.sqlite3","SELECT id,name FROM card_names"):new System.Data.DataTable();
  var notes=File.Exists(Data.FileAt("business-notes.sqlite3"))?Data.Table("business-notes.sqlite3","SELECT id,called,comment FROM notes"):new System.Data.DataTable();
  foreach(var c in cards){var n=names.Rows.Cast<System.Data.DataRow>().FirstOrDefault(r=>r[0].ToString()==c.Id);var note=notes.Rows.Cast<System.Data.DataRow>().FirstOrDefault(r=>r[0].ToString()==c.Id);Data.Execute("INSERT OR IGNORE INTO contacts(id,name,person,phones,email,category,source,description,called,note) VALUES($id,$name,$person,$phones,$email,$category,$source,$description,$called,$note)",("$id",c.Id),("$name",c.Name),("$person",n?[1].ToString()??""),("$phones",string.Join("; ",c.Phones)),("$email",c.Email),("$category",c.Category),("$source",c.Url),("$description",c.Body),("$called",note==null?0:Convert.ToInt32(note[1])),("$note",note?[2].ToString()??""));}
 }
 public static string Person(string phone){string Norm(string x)=>new string(x.Where(char.IsDigit).ToArray());var n=Norm(phone);if(n=="")return "";var t=Data.Table("assistant.sqlite3","SELECT person,phones FROM contacts WHERE person<>''");var matches=t.Rows.Cast<System.Data.DataRow>().Where(r=>r[1].ToString()!.Split(';').Any(p=>Norm(p)==n)).Select(r=>r[0].ToString()!).Distinct().ToArray();return matches.Length==1?matches[0]:"";}
}
