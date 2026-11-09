using System.Data;
using System.Diagnostics;
using System.Text.Json.Nodes;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace GaevAssistant;
sealed partial class MainWindow:Form {
 readonly FlowLayoutPanel statusPanel=new();readonly Engine engine=new();readonly Label state=new();readonly TextBox log=new();readonly Button start=new(),stop=new();readonly TabControl tabs=new();readonly NotifyIcon tray=new();bool closing;bool stopping;
 readonly TextBox search=new(),person=new(),note=new(),description=new();readonly ComboBox phone=new();readonly CheckBox called=new(),interested=new();readonly DataGridView contacts=Grid(),history=Grid();DataTable? contactData;DataRow? selected;
 CancellationTokenSource? playbackStop;WaveOutEvent? player;AudioFileReader? reader;string? mediaFile;
 public MainWindow(){Text="GAEV • Помощник для звонков";ClientSize=new(1080,760);MinimumSize=new(940,660);Font=new("Segoe UI",10);BackColor=Color.FromArgb(244,247,251);StartPosition=FormStartPosition.CenterScreen;
  var root=new TableLayoutPanel{Dock=DockStyle.Fill,Padding=new(16),RowCount=2,ColumnCount=1};root.RowStyles.Add(new(SizeType.Absolute,90));root.RowStyles.Add(new(SizeType.Percent,100));Controls.Add(root);
  var bar=statusPanel;bar.Dock=DockStyle.Fill;bar.Padding=new(12);bar.WrapContents=true;bar.BackColor=Color.FromArgb(232,237,243);root.Controls.Add(bar,0,0);state.Text="Помощник выключен";state.Size=new(365,52);state.Font=new("Segoe UI Semibold",11);bar.Controls.Add(state);Style(start,"Включить ожидание");Style(stop,"Остановить");bar.Controls.Add(start);bar.Controls.Add(stop);stop.Enabled=false;
  var dial=Button("Позвонить",()=>{
   var number=phone.SelectedItem?.ToString()??"";
   if(System.Text.RegularExpressions.Regex.IsMatch(number,@"^\+?[0-9]+$"))Open("tel:"+number);
  });
  dial.BackColor=Color.FromArgb(17,104,77);dial.ForeColor=Color.White;dial.FlatAppearance.BorderColor=dial.BackColor;
  dial.Enabled=false;phone.SelectedIndexChanged+=(s,e)=>dial.Enabled=phone.SelectedIndex>=0;
  bar.Controls.Add(dial);
  start.Click+=(s,e)=>{StopPlayback();engine.Start();RefreshState();};stop.Click+=async(s,e)=>await StopEngine();
  tabs.DrawMode=TabDrawMode.OwnerDrawFixed;
  tabs.SizeMode=TabSizeMode.Normal; tabs.Font=new Font(Font.FontFamily,12,FontStyle.Regular); tabs.Padding=new Point(17,9); tabs.Multiline=true;
  tabs.ItemSize=new Size(0,47);
  tabs.DrawItem+=(sender,args)=>{
   var title=tabs.TabPages[args.Index].Text;
   var accent=title switch{
    "Контакты"=>Color.FromArgb(10,139,91),
    "История и записи"=>Color.FromArgb(35,104,198),
    "Сценарии"=>Color.FromArgb(139,66,179),
    _=>Color.FromArgb(64,75,88)
   };
   bool active=args.Index==tabs.SelectedIndex;
   var bounds=args.Bounds;
   bounds.Inflate(-1,-1);
   var background=Color.White;
   using var fill=new SolidBrush(background);
   args.Graphics.FillRectangle(fill,bounds);
   using var border=new Pen(accent,active?4f:3f);border.Alignment=System.Drawing.Drawing2D.PenAlignment.Inset;args.Graphics.DrawRectangle(border,bounds.X,bounds.Y,bounds.Width-1,bounds.Height-1);
   var textArea=Rectangle.Inflate(bounds,-7,-4);
   TextRenderer.DrawText(args.Graphics,title,tabs.Font,textArea,Color.FromArgb(40,47,54),TextFormatFlags.HorizontalCenter|TextFormatFlags.VerticalCenter|TextFormatFlags.SingleLine);
   if(tabs.Focused && active)ControlPaint.DrawFocusRectangle(args.Graphics,Rectangle.Inflate(bounds,-4,-4),accent,background);
  };
  tabs.SelectedIndexChanged+=(s,e)=>tabs.Invalidate();
  tabs.Dock=DockStyle.Fill;root.Controls.Add(tabs,0,1);BuildContacts();BuildHistory();BuildScenarios();BuildSettings();BuildDiagnostics();
  engine.SessionSaved+=()=>{if(IsHandleCreated && !IsDisposed) BeginInvoke(LoadHistory);};
  engine.Changed+=text=>{if(!IsDisposed&&IsHandleCreated)BeginInvoke(()=>{if(!text.StartsWith("Диагностика"))state.Text=text;log.AppendText(DateTime.Now.ToString("HH:mm:ss")+"  "+text+Environment.NewLine);RefreshState();});};
  Icon=System.Drawing.Icon.ExtractAssociatedIcon(Environment.ProcessPath!);tray.Icon=Icon;tray.Text="GAEV • Помощник";tray.Visible=true;tray.DoubleClick+=(s,e)=>{Show();WindowState=FormWindowState.Normal;Activate();};var menu=new ContextMenuStrip();menu.Items.Add("Открыть",null,(s,e)=>{Show();Activate();});menu.Items.Add("Выход",null,async(s,e)=>await Exit());tray.ContextMenuStrip=menu;
  FormClosing+=(s,e)=>{if(!closing){e.Cancel=true;Hide();}};Shown+=async(s,e)=>await Diagnose();
 }
 static void Style(Button b,string text){b.Text=text;b.AutoSize=true;b.Height=38;b.Padding=new(10,3,10,3);b.FlatStyle=FlatStyle.Flat;b.FlatAppearance.BorderColor=Color.FromArgb(211,221,234);b.BackColor=Color.White;b.ForeColor=Color.FromArgb(25,70,140);b.Cursor=Cursors.Hand;}
 static Button Button(string text,Action action){var b=new Button();Style(b,text);b.Click+=(s,e)=>action();return b;}
 static DataGridView Grid()=>new(){Dock=DockStyle.Fill,ReadOnly=true,AllowUserToAddRows=false,AllowUserToDeleteRows=false,MultiSelect=false,SelectionMode=DataGridViewSelectionMode.FullRowSelect,AutoSizeColumnsMode=DataGridViewAutoSizeColumnsMode.Fill,RowHeadersVisible=false,BackgroundColor=Color.White,BorderStyle=BorderStyle.None};
 TabPage Page(string title){var p=new TabPage(title){Padding=new(14),BackColor=Color.White};tabs.TabPages.Add(p);return p;}
 void RefreshState(){var phase=stopping?RunPhase.Stopping:engine.Phase;statusPanel.BackColor=phase switch{RunPhase.Preparing or RunPhase.Connecting or RunPhase.Stopping=>Color.FromArgb(255,238,185),RunPhase.Ready=>Color.FromArgb(216,233,255),RunPhase.Live=>Color.FromArgb(204,244,220),RunPhase.Error=>Color.FromArgb(255,218,218),_=>Color.FromArgb(232,237,243)};state.ForeColor=phase switch{RunPhase.Error=>Color.FromArgb(145,20,30),RunPhase.Live=>Color.FromArgb(10,95,50),RunPhase.Ready=>Color.FromArgb(20,65,135),_=>Color.FromArgb(60,60,65)};start.Enabled=!engine.Enabled&&!stopping;stop.Enabled=engine.Enabled&&!stopping;}
 async Task StopEngine(){if(stopping)return;stopping=true;RefreshState();try{await engine.Stop();}catch(Exception e){MessageBox.Show(e.Message);}finally{stopping=false;RefreshState();}}
 async Task Exit(){await StopEngine();if(engine.Enabled)return;StopPlayback();closing=true;tray.Dispose();Close();}
 public static void Open(string url){try{Process.Start(new ProcessStartInfo(url){UseShellExecute=true});}catch(Exception e){MessageBox.Show(e.Message);}}
 void BuildContacts(){var p=Page("Контакты");var layout=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=1,RowCount=3};layout.RowStyles.Add(new(SizeType.AutoSize));layout.RowStyles.Add(new(SizeType.Percent,100));layout.RowStyles.Add(new(SizeType.AutoSize));p.Controls.Add(layout);search.PlaceholderText="Поиск по названию, имени, телефону и email";search.Dock=DockStyle.Fill;layout.Controls.Add(contacts,0,1);
  contactData=Data.Table("assistant.sqlite3","SELECT * FROM contacts ORDER BY name");contacts.DataSource=contactData;foreach(DataGridViewColumn c in contacts.Columns)c.Visible=new[]{"name","person","phones","email"}.Contains(c.Name);contacts.Columns["name"]!.HeaderText="Контакт";contacts.Columns["person"]!.HeaderText="Обращение";contacts.Columns["phones"]!.HeaderText="Телефоны";contacts.Columns["email"]!.HeaderText="Email";
  var filters=new TableLayoutPanel{AutoSize=true,Dock=DockStyle.Fill,ColumnCount=3,RowCount=1};
  filters.ColumnStyles.Add(new(SizeType.Percent,100));
  filters.ColumnStyles.Add(new(SizeType.Absolute,250));
  filters.ColumnStyles.Add(new(SizeType.Absolute,165));
  var section=new ComboBox{Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList};
  section.Items.Add("Все разделы");
  foreach(var group in contactData.AsEnumerable().Select(r=>r.Field<string>("category")).Where(c=>!string.IsNullOrEmpty(c)).Distinct().Order()) section.Items.Add(group!);
  var progress=new ComboBox{Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList};
  progress.Items.AddRange(new[]{"Все контакты","Ещё не звонили","Уже звонили","Есть интерес"});
  filters.Controls.Add(search,0,0);filters.Controls.Add(section,1,0);filters.Controls.Add(progress,2,0);layout.Controls.Add(filters,0,0);
  void Filter(){
   var parts=new List<string>();
   if(section.SelectedIndex>0)parts.Add("category='"+section.Text.Replace("'","''")+"'");
   if(progress.SelectedIndex>0)parts.Add(progress.SelectedIndex switch {1=>"called=0",2=>"called<>0",_=>"interested=1"});
   var q=search.Text.Replace("'","''").Replace("[","[[]").Replace("%","[%]").Replace("*","[*]");
   if(q.Length>0)parts.Add($"(name LIKE '%{q}%' OR person LIKE '%{q}%' OR phones LIKE '%{q}%' OR email LIKE '%{q}%')");
   contactData.DefaultView.RowFilter=string.Join(" AND ",parts);
  }
  section.SelectedIndex=section.Items.Contains("Emall")?section.Items.IndexOf("Emall"):0;
  progress.SelectedIndex=1;
  search.TextChanged+=(s,e)=>Filter();section.SelectedIndexChanged+=(s,e)=>Filter();progress.SelectedIndexChanged+=(s,e)=>Filter();Filter();
  var fields=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=2,RowCount=5};fields.ColumnStyles.Add(new(SizeType.Percent,50));fields.ColumnStyles.Add(new(SizeType.Percent,50));fields.AutoSize=true;for(int i=0;i<5;i++)fields.RowStyles.Add(new(SizeType.AutoSize));layout.Controls.Add(fields,0,2);
  description.Multiline=true;description.ReadOnly=true;description.ScrollBars=ScrollBars.Vertical;description.Dock=DockStyle.Fill;fields.Controls.Add(description,0,0);fields.SetRowSpan(description,3);person.PlaceholderText="Имя собеседника для агента";person.MaxLength=80;person.Dock=DockStyle.Fill;fields.Controls.Add(person,1,0);phone.DropDownStyle=ComboBoxStyle.DropDownList;phone.Dock=DockStyle.Fill;fields.Controls.Add(phone,1,1);note.Multiline=true;note.Height=note.Font.Height*2+10;note.ScrollBars=ScrollBars.Vertical;note.PlaceholderText="Заметка о контакте";note.Dock=DockStyle.Fill;fields.Controls.Add(note,1,2);called.Text="Уже звонили";called.AutoSize=true;var flags=new FlowLayoutPanel{AutoSize=true,Dock=DockStyle.Fill};interested.Text="Есть интерес";interested.AutoSize=true;flags.Controls.Add(called);flags.Controls.Add(interested);fields.Controls.Add(flags,1,3);
  var sourceLink=new LinkLabel{AutoSize=true,Text="Источник / объявление",Anchor=AnchorStyles.Left};
  fields.Controls.Add(sourceLink,0,3);
  void SaveField(string column,object value){
   var row=selected;
   if(loadingContact||row==null||Equals(row[column],value))return;
   Data.Execute("UPDATE contacts SET "+column+"=$value WHERE id=$id",("$value",value),("$id",row["id"]));
   row[column]=value;
  }
  person.TextChanged+=(s,e)=>SaveField("person",person.Text);
  note.TextChanged+=(s,e)=>SaveField("note",note.Text);
  interested.Click+=(s,e)=>SaveField("interested",interested.Checked?1L:0L);
  called.Click+=(s,e)=>SaveField("called",called.Checked?1L:0L);
  void RefreshLink(){sourceLink.Visible=Uri.TryCreate(selected?["source"].ToString(),UriKind.Absolute,out var u)&&u.Scheme is "http" or "https";}
  sourceLink.LinkClicked+=(s,e)=>{if(sourceLink.Visible)Open(selected!["source"].ToString()!);};
  contacts.SelectionChanged+=(s,e)=>{SelectContact();RefreshLink();};SelectContact();RefreshLink();
 }
 bool loadingContact;
 void SelectContact(){loadingContact=true;try{selected=(contacts.CurrentRow?.DataBoundItem as DataRowView)?.Row;person.Text=selected?["person"].ToString()??"";note.Text=selected?["note"].ToString()??"";description.Text=selected?["description"].ToString()??"";interested.Checked=selected!=null&&Convert.ToInt32(selected["interested"])!=0;called.Checked=selected!=null&&Convert.ToInt32(selected["called"])!=0;phone.Items.Clear();foreach(var n in (selected?["phones"].ToString()??"").Split(';',StringSplitOptions.RemoveEmptyEntries))phone.Items.Add(n.Trim());if(phone.Items.Count>0)phone.SelectedIndex=0;}finally{loadingContact=false;}}
 void BuildHistory(){var p=Page("История и записи");var actions=new FlowLayoutPanel{Dock=DockStyle.Top,Height=46};p.Controls.Add(history);p.Controls.Add(actions);actions.Controls.Add(Button("Обновить",LoadHistory));var play=Button("Слушать запись",()=>{});play.Click+=async(s,e)=>{play.Enabled=false;try{await Play();}catch(Exception ex){MessageBox.Show(ex is HttpRequestException?"Запись недоступна или ещё обрабатывается.":ex.Message);}finally{play.Enabled=true;}};actions.Controls.Add(play);actions.Controls.Add(Button("Остановить запись",StopPlayback));actions.Controls.Add(Button("Текст разговора",()=>{if(history.CurrentRow?.DataBoundItem is DataRowView r){var f=new Form{Text="Текст разговора",Size=new(700,600)};f.Controls.Add(new TextBox{Dock=DockStyle.Fill,Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Text=r["transcript"].ToString()});f.Show(this);}}));LoadHistory();}
 void LoadHistory(){var t=Data.Table("assistant.sqlite3","SELECT started,name,phone,conversation,result,transcript FROM sessions ORDER BY started DESC");if(File.Exists(Data.FileAt("crm.sqlite3"))){var old=Data.Table("crm.sqlite3","SELECT started_at,name,phone,conversation_id,end_reason,COALESCE((SELECT group_concat(role || ': ' || text,char(10)) FROM turns WHERE call_id=calls.id),'') FROM calls WHERE ended_at IS NOT NULL ORDER BY started_at DESC");foreach(DataRow r in old.Rows)t.Rows.Add(r[0],r[1],r[2],r[3],r[4],r[5]);}history.DataSource=t;history.Columns["transcript"]!.Visible=false;history.Columns["conversation"]!.Visible=false;history.Columns["started"]!.HeaderText="Дата";history.Columns["name"]!.HeaderText="Имя";history.Columns["phone"]!.HeaderText="Телефон";history.Columns["result"]!.HeaderText="Завершение";}
 async Task Play(){StopPlayback();var p=await Phone.Read(CancellationToken.None);if(p.Phase!="idle")throw new Exception("Прослушивание недоступно во время звонка.");var id=(history.CurrentRow?.DataBoundItem as DataRowView)?["conversation"].ToString()??"";if(!System.Text.RegularExpressions.Regex.IsMatch(id,@"^conv_[A-Za-z0-9_-]+$"))throw new Exception("У этого разговора нет записи ElevenLabs.");playbackStop=new();var owner=playbackStop;using var http=Data.Http();var bytes=await DownloadRecording(http,id,owner.Token);owner.Token.ThrowIfCancellationRequested();p=await Phone.Read(owner.Token);if(p.Phase!="idle")throw new Exception("Начался звонок; воспроизведение отменено.");mediaFile=Data.FileAt("recording-"+Guid.NewGuid().ToString("N")+".mp3");await File.WriteAllBytesAsync(mediaFile,bytes,owner.Token);reader=new AudioFileReader(mediaFile);player=new WaveOutEvent();player.Init(reader);player.Play();_ = WatchPlayback(owner);}
 static async Task<byte[]> DownloadRecording(HttpClient http,string id,CancellationToken ct){using var response=await http.GetAsync("https://api.elevenlabs.io/v1/convai/conversations/"+id+"/audio",HttpCompletionOption.ResponseHeadersRead,ct);response.EnsureSuccessStatusCode();const int limit=50*1024*1024;if(response.Content.Headers.ContentLength>limit)throw new Exception("Запись больше 50 МБ.");using var input=await response.Content.ReadAsStreamAsync(ct);using var data=new MemoryStream();var buf=new byte[65536];int n;while((n=await input.ReadAsync(buf,ct))>0){if(data.Length+n>limit)throw new Exception("Запись больше 50 МБ.");data.Write(buf,0,n);}if(data.Length==0)throw new Exception("Запись пустая.");return data.ToArray();}
 async Task WatchPlayback(CancellationTokenSource owner){try{while(!owner.IsCancellationRequested&&player?.PlaybackState==PlaybackState.Playing){await Task.Delay(500,owner.Token);var p=await Phone.Read(owner.Token);if(p.Phase!="idle")break;}}catch{}finally{if(playbackStop==owner)StopPlayback();}}
 void StopPlayback(){playbackStop?.Cancel();playbackStop=null;player?.Stop();player?.Dispose();player=null;reader?.Dispose();reader=null;if(mediaFile!=null){try{File.Delete(mediaFile);}catch{}mediaFile=null;}}
 void BuildSettings(){var p=Page("Настройки");var box=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=2,RowCount=7};box.ColumnStyles.Add(new(SizeType.Absolute,150));box.ColumnStyles.Add(new(SizeType.Percent,100));p.Controls.Add(box);JsonObject config;try{config=Data.Secrets("eleven.dpapi");}catch{config=new();}var fields=new Dictionary<string,TextBox>();int row=0;foreach(var pair in new[]{("agentId","ID агента"),("elevenKey","API-ключ"),("voiceId","ID голоса"),("greeting","Приветствие")}){box.RowStyles.Add(new(row==4?SizeType.Percent:SizeType.Absolute,row==4?100:42));box.Controls.Add(new Label{Text=pair.Item2,AutoSize=true},0,row);var f=new TextBox{Text=Data.S(config,pair.Item1),Dock=DockStyle.Fill,Multiline=row==4,ScrollBars=row==4?ScrollBars.Vertical:ScrollBars.None,UseSystemPasswordChar=pair.Item1=="elevenKey"};fields[pair.Item1]=f;box.Controls.Add(f,1,row++);}box.RowStyles.Add(new(SizeType.Absolute,44));box.Controls.Add(Button("Сохранить настройки",()=>{if(engine.Enabled){MessageBox.Show("Сначала остановите ожидание.");return;}try{config=Data.Secrets("eleven.dpapi");}catch(FileNotFoundException){config=new();}foreach(var pair in fields)config[pair.Key]=pair.Value.Text;Data.SaveSecrets("eleven.dpapi",config);MessageBox.Show("Сохранено в зашифрованном хранилище Windows.");}),1,5);box.Controls.Add(Button("Настроить прокси",()=>{if(engine.Enabled){MessageBox.Show("Сначала остановите ожидание.");return;}using var f=new ProxySettings();f.ShowDialog(this);}),0,6);box.RowStyles.Add(new(SizeType.Absolute,56));box.Controls.Add(new Label{Text="API и записи используют собственный прокси приложения. Настройки перенесены из прежней программы. Контакты и история хранятся на этом компьютере.",Dock=DockStyle.Fill},1,6);}
 void BuildDiagnostics(){var p=Page("Проверка и журнал");log.Multiline=true;log.ReadOnly=true;log.ScrollBars=ScrollBars.Vertical;log.Dock=DockStyle.Fill;p.Controls.Add(log);var actions=new FlowLayoutPanel{Dock=DockStyle.Top,Height=50};p.Controls.Add(actions);var check=Button("Проверить",()=>{});check.Click+=async(s,e)=>{check.Enabled=false;try{await Diagnose();}finally{check.Enabled=true;}};actions.Controls.Add(check);actions.Controls.Add(Button("Скачать VB-CABLE",()=>Open("https://vb-audio.com/Cable/")));actions.Controls.Add(Button("Открыть Связь с телефоном",()=>Open("ms-phone:")));actions.Controls.Add(Button("Открыть журналы",()=>{Directory.CreateDirectory(Data.FileAt("logs"));Open(Data.FileAt("logs"));}));}
 async Task Diagnose(){void Write(string s)=>log.AppendText(DateTime.Now.ToString("HH:mm:ss")+"  "+s+Environment.NewLine);try{using var a=MicLease.Cable(DataFlow.Capture);using var b=MicLease.Cable(DataFlow.Render);Write("VB-CABLE: оба устройства доступны.");}catch(Exception e){Write(e.Message);}Write(Process.GetProcessesByName("PhoneExperienceHost").Length>0?"Связь с телефоном запущена.":"Запустите Связь с телефоном.");try{var p=await Phone.Read(CancellationToken.None);Write("Windows API телефона: "+p.Phase);}catch(Exception e){Write("Телефон: "+e.Message);}try{using var http=Data.Http();var config=Data.Secrets("eleven.dpapi");using var r=await http.GetAsync("https://api.elevenlabs.io/v1/convai/agents/"+Uri.EscapeDataString(Data.S(config,"agentId")));Write("ElevenLabs через прокси: HTTP "+(int)r.StatusCode);}catch{Write("Не удалось обратиться к ElevenLabs через прокси. Проверьте подключение и настройки.");}Write("Контактов: "+contactData?.Rows.Count+". Служба старой программы не используется.");}
}
