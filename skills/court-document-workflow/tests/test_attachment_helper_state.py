"""Compile helper logic with synthetic attachments; never opens an EventStore."""
import json,pathlib,subprocess,sys,tempfile,unittest

SOURCE=pathlib.Path(__file__).resolve().parents[1]/'scripts/runtime/court_event_attachment.m'

@unittest.skipUnless(sys.platform=='darwin','EventKit Objective-C runtime only exists on macOS')
class AttachmentHelperStateTests(unittest.TestCase):
 def test_sync_evidence_and_strict_repair_without_calendar_access(self):
  with tempfile.TemporaryDirectory(prefix='attachment-helper-test-') as directory:
   root=pathlib.Path(directory);pdf=root/'source.pdf';pdf.write_bytes(b'%PDF-1.4\nsynthetic\n%%EOF')
   source=root/'test.m';source.write_text('#define main production_main\n#include "'+str(SOURCE)+'"\n#undef main\n'+r'''
@interface FakeSource:NSObject
@property EKSourceType sourceType;
@property NSString *title;
@end
@implementation FakeSource @end
@interface FakeCalendar:NSObject
@property FakeSource *source;
@end
@implementation FakeCalendar @end
@interface FakeAttachment:NSObject
@property NSURL *URL;
@property NSURL *localURL;
@property NSString *UUID;
@property NSString *fileName;
@end
@implementation FakeAttachment @end
@interface FakeEvent:NSObject
@property FakeCalendar *calendar;
@property NSArray *attachments;
@end
@implementation FakeEvent
- (void)addAttachment:(id)value { abort(); }
- (void)removeAttachment:(id)value { abort(); }
@end
static int scopeStarts=0,scopeStops=0;
static BOOL TestScopeStart(id object,SEL selector) { scopeStarts++;return YES; }
static void TestScopeStop(id object,SEL selector) { scopeStops++; }
int main(int argc,const char **argv) { @autoreleasepool {
 FakeSource *source=[FakeSource new];source.sourceType=EKSourceTypeCalDAV;source.title=@"iCloud";
 FakeCalendar *calendar=[FakeCalendar new];calendar.source=source;
 FakeAttachment *attachment=[FakeAttachment new];attachment.UUID=@"before";attachment.fileName=@"source.pdf";
 attachment.localURL=[NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
 assert(!AttachmentRemoteAvailable(attachment));
 attachment.URL=attachment.localURL;assert(!AttachmentRemoteAvailable(attachment));
 attachment.URL=[NSURL URLWithString:@"https://gateway.icloud.com/synthetic-not-real"];
 assert(AttachmentRemoteAvailable(attachment));attachment.URL=nil;
 assert([AttachmentSyncScope((id)calendar) isEqual:@"icloud"]);
 NSMutableDictionary *status=[NSMutableDictionary dictionary];
 AddAttachmentSyncStatus(status,@"icloud",@[@{@"remote_available":@NO}]);
 assert([status[@"attachment_sync_state"] isEqual:@"pending"] && [status[@"sync_pending"] boolValue]);
 NSDictionary *pendingStatus=[status copy];
 AddAttachmentSyncStatus(status,@"icloud",@[@{@"remote_available":@YES}]);
 assert([status[@"attachment_sync_state"] isEqual:@"uploaded"] && ![status[@"sync_pending"] boolValue]);
 NSMutableDictionary *local=[NSMutableDictionary dictionary];AddAttachmentSyncStatus(local,@"local",@[]);
 assert(local[@"attachment_sync_state"]==nil);
 FakeEvent *event=[FakeEvent new];event.calendar=calendar;event.attachments=@[attachment];
 NSString *sha=AttachmentSHA(attachment);assert(sha.length==64);
 Method startMethod=class_getInstanceMethod([NSURL class],@selector(startAccessingSecurityScopedResource));
 Method stopMethod=class_getInstanceMethod([NSURL class],@selector(stopAccessingSecurityScopedResource));
 IMP oldStart=method_setImplementation(startMethod,(IMP)TestScopeStart),oldStop=method_setImplementation(stopMethod,(IMP)TestScopeStop);
 assert([AttachmentSHA(attachment) isEqual:sha]);assert(scopeStarts==1 && scopeStops==1);
 method_setImplementation(startMethod,oldStart);method_setImplementation(stopMethod,oldStop);
 NSArray *before=AttachmentInventory((id)event);
 NSDictionary *readSource=@{@"source_path":attachment.localURL.path,@"sha256":sha,@"file_name":@"source.pdf"};
 NSArray *unreadable=@[@{@"attachment_uuid":@"pending",@"sha256":[NSNull null],@"file_name":@"source.pdf",@"remote_available":@NO}];
 assert(HasUnreadableSameNameAttachment(readSource,unreadable));
 assert(!HasUnreadableSameNameAttachment(readSource,before));
 assert(!HasUnreadableSameNameAttachment(readSource,@[@{@"sha256":[NSNull null],@"file_name":@"other.pdf"}]));
 assert(VerifiedReviewAttachments((id)event,@[readSource],@"icloud").count==1);
 assert(VerifiedReviewAttachments((id)event,@[@{@"sha256":@"wrong",@"file_name":@"source.pdf"}],@"icloud")==nil);
 __block int attempts=0,pauses=0;__block double totalWait=0;
 int retryResult=BoundedAttachmentReadback(^int { attempts++;return attempts<3 ? 75 : 0; },^(NSTimeInterval seconds) { pauses++;totalWait+=seconds; });
 assert(retryResult==0 && attempts==3 && pauses==2 && totalWait<1);
 attempts=0;pauses=0;totalWait=0;
 retryResult=BoundedAttachmentReadback(^int { attempts++;return 75; },^(NSTimeInterval seconds) { pauses++;totalWait+=seconds; });
 assert(retryResult==75 && attempts==6 && pauses==5 && totalWait<7);
 attempts=0;pauses=0;
 retryResult=BoundedAttachmentReadback(^int { attempts++;return 73; },^(NSTimeInterval seconds) { pauses++; });
 assert(retryResult==73 && attempts==1 && pauses==0);
 assert(ExistingAttachmentsPreserved(before,before));
 assert(!ExistingAttachmentsPreserved(before,@[]));
 NSMutableDictionary *changedOld=[before[0] mutableCopy];changedOld[@"sha256"]=@"changed";
 assert(!ExistingAttachmentsPreserved(before,@[changedOld]));
 NSDictionary *request=@{@"attachment_uuid":@"before",@"sha256":sha,@"source_path":attachment.localURL.path};
 NSMutableDictionary *plan=[@{@"repair_authorization_id":@"operator-reviewed-local-upload-repair-v1",@"operator_confirmed_local_repair":@YES,
  @"case_number":@"（2026）测0101民初123号",@"title":@"（2026）测0101民初123号 开庭",@"notes":@"手工备注保留",
  @"expected_attachment_inventory":before,@"repair_attachments":@[request]} mutableCopy];
 NSArray *sources=@[@{@"source_path":attachment.localURL.path,@"sha256":sha}];NSString *failure=nil;
 NSArray *repairs=PrepareAttachmentRepairs((id)event,plan,sources,&failure);
 assert(repairs.count==1); // Only allocates a replacement; FakeEvent mutation methods abort.
 plan[@"operator_confirmed_local_repair"]=@NO;assert(PrepareAttachmentRepairs((id)event,plan,sources,&failure)==nil);
 plan[@"operator_confirmed_local_repair"]=@YES;
 NSArray *after=@[@{@"attachment_uuid":@"after",@"sha256":sha,@"file_name":@"source.pdf",@"remote_available":@NO}];
 assert(VerifyAttachmentRepairs(before,after,repairs));
 assert(!VerifyAttachmentRepairs(before,before,repairs));
 assert(!VerifyAttachmentRepairs(before,@[],repairs));
 NSDictionary *remote=@{@"attachment_uuid":@"keep-remote",@"sha256":@"other-sha",@"file_name":@"other.pdf",@"remote_available":@YES};
 NSArray *withRemote=@[before[0],remote], *afterWithRemote=@[after[0],remote];
 assert(VerifyAttachmentRepairs(withRemote,afterWithRemote,repairs));
 NSMutableDictionary *lostRemote=[remote mutableCopy];lostRemote[@"remote_available"]=@NO;
 assert(!VerifyAttachmentRepairs(withRemote,@[after[0],lostRemote],repairs));
 lostRemote=[remote mutableCopy];lostRemote[@"attachment_uuid"]=@"replaced-unasked";
 assert(!VerifyAttachmentRepairs(withRemote,@[after[0],lostRemote],repairs));
 attachment.URL=[NSURL URLWithString:@"https://gateway.icloud.com/synthetic-not-real"];
 plan[@"expected_attachment_inventory"]=AttachmentInventory((id)event);
 assert(PrepareAttachmentRepairs((id)event,plan,sources,&failure)==nil); // Never replace a remote attachment.
 attachment.URL=nil;plan[@"expected_attachment_inventory"]=before;
 plan[@"repair_attachments"]=@[request,request];assert(PrepareAttachmentRepairs((id)event,plan,sources,&failure)==nil);
 plan[@"repair_attachments"]=@[request];plan[@"case_number"]=@"（2026）测0101民初999号";
 assert(PrepareAttachmentRepairs((id)event,plan,sources,&failure)==nil);
 plan[@"case_number"]=@"（2026）测0101民初123号";plan[@"expected_attachment_inventory"]=@[];
 assert(PrepareAttachmentRepairs((id)event,plan,sources,&failure)==nil);
 NSDictionary *roundtrip=@{@"pending":pendingStatus,@"uploaded":status,@"local":local,
  @"remote_available":@(AttachmentRemoteAvailable(attachment))};
 NSData *json=[NSJSONSerialization dataWithJSONObject:roundtrip options:0 error:nil];
 printf("%s\n",[[[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding] UTF8String]);
 return 0;
} }
''')
   binary=root/'test'
   subprocess.run(['/usr/bin/clang','-fobjc-arc','-fblocks','-framework','Foundation','-framework','EventKit',str(source),'-o',str(binary)],check=True,capture_output=True,text=True)
   result=subprocess.run([str(binary),str(pdf)],check=True,capture_output=True,text=True)
   roundtrip=json.loads(result.stdout)
   self.assertIs(roundtrip['pending']['sync_pending'],True)
   self.assertIs(roundtrip['uploaded']['sync_pending'],False)
   self.assertIsInstance(roundtrip['remote_available'],bool)
   self.assertNotIn('sync_pending',roundtrip['local'])

if __name__=='__main__':unittest.main()
