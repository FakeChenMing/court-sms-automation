#import <Foundation/Foundation.h>
#import <EventKit/EventKit.h>
#import <objc/message.h>
#import <objc/runtime.h>
#import <CommonCrypto/CommonDigest.h>

static BOOL EnsureCalendarAccess(EKEventStore *store, NSError **error) {
    EKAuthorizationStatus status = [EKEventStore authorizationStatusForEntityType:EKEntityTypeEvent];
    if (status == EKAuthorizationStatusFullAccess) {
        return YES;
    }
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    __block BOOL granted = NO;
    __block NSError *accessError = nil;
    [store requestFullAccessToEventsWithCompletion:^(BOOL success, NSError *requestError) {
        granted = success;
        accessError = requestError;
        dispatch_semaphore_signal(semaphore);
    }];
    if (dispatch_semaphore_wait(semaphore, dispatch_time(DISPATCH_TIME_NOW, 120 * NSEC_PER_SEC)) != 0) {
        if (error) *error = [NSError errorWithDomain:@"CourtEventAttachment" code:1 userInfo:@{NSLocalizedDescriptionKey: @"等待日历授权超时"}];
        return NO;
    }
    if (!granted && error) *error = accessError ?: [NSError errorWithDomain:@"CourtEventAttachment" code:2 userInfo:@{NSLocalizedDescriptionKey: @"未获得日历访问权限"}];
    return granted;
}

static id SendId(id target, NSString *selectorName, id argument) {
    SEL selector = NSSelectorFromString(selectorName);
    return ((id (*)(id, SEL, id))objc_msgSend)(target, selector, argument);
}

static void SendVoid(id target, NSString *selectorName, id argument) {
    SEL selector = NSSelectorFromString(selectorName);
    ((void (*)(id, SEL, id))objc_msgSend)(target, selector, argument);
}

static EKCalendar *CalendarNamed(EKEventStore *store, NSString *calendarName) {
    for (EKCalendar *candidate in [store calendarsForEntityType:EKEntityTypeEvent]) {
        if ([candidate.title isEqualToString:calendarName]) return candidate;
    }
    return nil;
}

// Review workflow: metadata is written by Calendar scripting; this operation only
// adds verified source files and a relative alarm to one exact marked event.
static NSString *SHA256Data(NSData *data) {
    if (!data) return nil;
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, hash);
    NSMutableString *hex = [NSMutableString string];
    for (int i=0; i<CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", hash[i]];
    return hex;
}

static NSArray *ReviewAttachments(EKEvent *event) {
    if (![event respondsToSelector:NSSelectorFromString(@"attachments")]) return nil;
    return ((id (*)(id, SEL))objc_msgSend)(event, NSSelectorFromString(@"attachments")) ?: @[];
}

static NSString *AttachmentSHA(id attachment) {
    for (NSString *name in @[@"localURL", @"urlOnDisk", @"URLForPendingFileCopy"]) {
        SEL sel = NSSelectorFromString(name);
        if (![attachment respondsToSelector:sel]) continue;
        id value = ((id (*)(id, SEL))objc_msgSend)(attachment, sel);
        NSURL *url = [value isKindOfClass:[NSURL class]] ? value : nil;
        if (url.isFileURL) {
            BOOL accessed=[url startAccessingSecurityScopedResource];
            NSString *hash=nil;
            @try { hash=SHA256Data([NSData dataWithContentsOfURL:url]); }
            @finally { if (accessed) [url stopAccessingSecurityScopedResource]; }
            if (hash) return hash;
        }
    }
    return nil;
}

// A local attachment copy and an iCloud remote reference are separate evidence.
// Never print access-bearing URLs, and never treat a pending upload as a missing
// PDF (which would add a duplicate on the next retry).
static BOOL AttachmentRemoteAvailable(id attachment) {
    for (NSString *name in @[@"URL", @"url"]) {
        SEL selector=NSSelectorFromString(name);
        if (![attachment respondsToSelector:selector]) continue;
        @try {
            id value=((id (*)(id, SEL))objc_msgSend)(attachment,selector);
            NSURL *url=[value isKindOfClass:[NSURL class]] ? value :
                ([value isKindOfClass:[NSString class]] ? [NSURL URLWithString:value] : nil);
            if (url.host.length && [@[@"https",@"http"] containsObject:url.scheme.lowercaseString]) return YES;
        } @catch (NSException *exception) { /* Unavailable remote metadata remains pending. */ }
    }
    return NO;
}

static NSString *AttachmentSyncScope(EKCalendar *calendar) {
    EKSource *source=calendar.source;
    if (!source) return @"other";
    if (source.sourceType==EKSourceTypeLocal) return @"local";
    // Only the identified iCloud service gets cloud upload status. Other CalDAV
    // providers (including renamed/unknown sources) are not silently classified.
    if (source.sourceType==EKSourceTypeCalDAV && [source.title caseInsensitiveCompare:@"iCloud"]==NSOrderedSame) return @"icloud";
    return @"other";
}

static void AddAttachmentSyncStatus(NSMutableDictionary *result, NSString *scope, NSArray *attachments) {
    result[@"attachment_sync_scope"]=scope;
    if (![scope isEqualToString:@"icloud"]) return;
    BOOL uploaded=attachments.count>0;
    for (NSDictionary *attachment in attachments) if (![attachment[@"remote_available"] boolValue]) uploaded=NO;
    result[@"attachment_sync_state"]=uploaded ? @"uploaded" : @"pending";
    result[@"sync_pending"]=uploaded ? @NO : @YES;
    // This proves a remote URL is registered, not receipt by another Mac.
    result[@"attachment_sync_evidence"]=@"remote_url_registered";
    result[@"attachment_sync_checked_at"]=@([NSDate date].timeIntervalSince1970);
}

static NSString *AttachmentIdentifier(id attachment) {
    SEL selector=NSSelectorFromString(@"UUID");
    if (![attachment respondsToSelector:selector]) return nil;
    id value=((id (*)(id,SEL))objc_msgSend)(attachment,selector);
    return [value isKindOfClass:[NSString class]] ? value : nil;
}

static NSArray *AttachmentInventory(EKEvent *event) {
    NSMutableArray *items=[NSMutableArray array];
    for (id attachment in ReviewAttachments(event)) {
        NSString *name=((id (*)(id,SEL))objc_msgSend)(attachment,NSSelectorFromString(@"fileName"));
        [items addObject:@{@"attachment_uuid":AttachmentIdentifier(attachment) ?: @"",
                          @"sha256":AttachmentSHA(attachment) ?: (id)[NSNull null],
                          @"file_name":name ?: @"",@"remote_available":@(AttachmentRemoteAvailable(attachment))}];
    }
    return items;
}

// Missing readable content is not evidence that the attachment is absent.
// An earlier save or an OS access restriction must never cause a second copy.
static BOOL HasUnreadableSameNameAttachment(NSDictionary *source, NSArray *inventory) {
    for (NSDictionary *item in inventory) if ([item[@"sha256"] isEqual:source[@"sha256"]]) return NO;
    for (NSDictionary *item in inventory) {
        if ([item[@"file_name"] isEqual:source[@"file_name"]]
            && (!item[@"sha256"] || item[@"sha256"]==[NSNull null])) return YES;
    }
    return NO;
}

static NSArray *VerifiedReviewAttachments(EKEvent *event, NSArray *sources, NSString *scope) {
    NSMutableArray *verified=[NSMutableArray array];
    for (NSDictionary *source in sources) {
        BOOL found=NO;
        for (id attachment in ReviewAttachments(event)) {
            if ([AttachmentSHA(attachment) isEqual:source[@"sha256"]]) {
                NSString *name=((id (*)(id,SEL))objc_msgSend)(attachment,NSSelectorFromString(@"fileName"));
                NSMutableDictionary *entry=[source mutableCopy];entry[@"file_name"]=name ?: source[@"file_name"];
                entry[@"content_verified"]=@YES;
                if ([scope isEqual:@"icloud"]) entry[@"remote_available"]=@(AttachmentRemoteAvailable(attachment));
                [verified addObject:entry];found=YES;break;
            }
        }
        if (!found) return nil;
    }
    return verified;
}

// Only content-not-yet-readable (75) is retried. Each actual callback creates a
// fresh EventStore and checks the exact UID/marker and all protected metadata.
static int BoundedAttachmentReadback(int (^attempt)(void), void (^pause)(NSTimeInterval)) {
    for (NSUInteger index=0;index<6;index++) {
        int status=attempt();
        if (status!=75 || index==5) return status;
        pause(0.2*(1UL<<index));
    }
    return 75;
}

static BOOL SameInventory(NSArray *left, NSArray *right) {
    return [left isKindOfClass:[NSArray class]] && [right isKindOfClass:[NSArray class]]
        && left.count==right.count && [[NSSet setWithArray:left] isEqualToSet:[NSSet setWithArray:right]];
}

static BOOL ExistingAttachmentsPreserved(NSArray *before, NSArray *after) {
    for (NSDictionary *old in before) {
        NSDictionary *found=nil;
        for (NSDictionary *item in after) if ([item[@"attachment_uuid"] isEqual:old[@"attachment_uuid"]]) { found=item;break; }
        if (!found || ![found[@"file_name"] isEqual:old[@"file_name"]]) return NO;
        if (old[@"sha256"] && old[@"sha256"]!=[NSNull null] && ![old[@"sha256"] isEqual:found[@"sha256"]]) return NO;
        if ([old[@"remote_available"] boolValue] && ![found[@"remote_available"] boolValue]) return NO;
    }
    return YES;
}

static BOOL RepairCaseMatches(NSDictionary *plan) {
    NSString *caseNumber=plan[@"case_number"];
    if (![caseNumber isKindOfClass:[NSString class]]) return NO;
    NSRegularExpression *format=[NSRegularExpression regularExpressionWithPattern:@"^（20[0-9]{2}）[\\p{Han}A-Za-z0-9]+号$" options:0 error:nil];
    if ([format numberOfMatchesInString:caseNumber options:0 range:NSMakeRange(0,caseNumber.length)]!=1) return NO;
    NSString *text=[NSString stringWithFormat:@"%@\n%@",plan[@"title"] ?: @"",plan[@"notes"] ?: @""];
    text=[[text stringByReplacingOccurrencesOfString:@"(" withString:@"（"] stringByReplacingOccurrencesOfString:@")" withString:@"）"];
    text=[[text componentsSeparatedByCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] componentsJoinedByString:@""];
    return [text containsString:caseNumber];
}

static NSArray *PrepareAttachmentRepairs(EKEvent *event, NSDictionary *plan, NSArray *sources, NSString **failure) {
    NSArray *inventory=AttachmentInventory(event), *requested=plan[@"repair_attachments"];
    NSString *authorization=@"operator-reviewed-local-upload-repair-v1";
    if (![plan[@"repair_authorization_id"] isEqual:authorization]
        || ![plan[@"operator_confirmed_local_repair"] isEqual:@YES]
        || !RepairCaseMatches(plan) || ![AttachmentSyncScope(event.calendar) isEqual:@"icloud"]
        || !SameInventory(inventory,plan[@"expected_attachment_inventory"])
        || ![requested isKindOfClass:[NSArray class]] || requested.count==0) {
        *failure=@"附件修复授权、案件或原附件快照不符";return nil;
    }
    NSMutableSet *allIDs=[NSMutableSet set], *selected=[NSMutableSet set];
    for (NSDictionary *item in inventory) {
        NSString *identifier=item[@"attachment_uuid"];
        if (!identifier.length || [allIDs containsObject:identifier]) { *failure=@"附件UUID不唯一";return nil; }
        [allIDs addObject:identifier];
    }
    Class cls=NSClassFromString(@"EKAttachment");
    if (![event respondsToSelector:NSSelectorFromString(@"removeAttachment:")]
        || ![event respondsToSelector:NSSelectorFromString(@"addAttachment:")]
        || ![cls instancesRespondToSelector:NSSelectorFromString(@"initWithFilepath:")]) {
        *failure=@"当前系统不支持原子替换附件";return nil;
    }
    NSMutableArray *replacements=[NSMutableArray array];
    for (NSDictionary *request in requested) {
        if (![request isKindOfClass:[NSDictionary class]]) { *failure=@"附件修复清单格式无效";return nil; }
        NSString *identifier=request[@"attachment_uuid"], *sha=request[@"sha256"], *path=request[@"source_path"];
        if (![identifier isKindOfClass:[NSString class]] || ![sha isKindOfClass:[NSString class]] || ![path isKindOfClass:[NSString class]]
            || [selected containsObject:identifier]) { *failure=@"附件修复清单重复或缺字段";return nil; }
        [selected addObject:identifier];
        BOOL sourceFound=NO;
        for (NSDictionary *source in sources) if ([source[@"source_path"] isEqual:path] && [source[@"sha256"] isEqual:sha]) sourceFound=YES;
        id original=nil;
        for (id attachment in ReviewAttachments(event)) if ([AttachmentIdentifier(attachment) isEqual:identifier]) original=attachment;
        BOOL attachmentAllowed=!AttachmentRemoteAvailable(original) && [AttachmentSHA(original) isEqual:sha];
        if (!sourceFound || !original || !attachmentAllowed) {
            *failure=@"附件不符合本机待上传的严格替换条件";return nil;
        }
        id replacement=SendId([cls alloc],@"initWithFilepath:",[NSURL fileURLWithPath:path]);
        if (!replacement) { *failure=@"替换附件创建失败";return nil; }
        [replacements addObject:@{@"old":original,@"new":replacement,@"before_uuid":identifier,@"sha256":sha}];
    }
    return replacements;
}

static BOOL VerifyAttachmentRepairs(NSArray *before, NSArray *after, NSArray *replacements) {
    if (before.count!=after.count) return NO;
    NSMutableSet *removed=[NSMutableSet set];
    NSMutableArray *expectedHashes=[NSMutableArray array], *actualHashes=[NSMutableArray array];
    for (NSDictionary *repair in replacements) [removed addObject:repair[@"before_uuid"]];
    for (NSDictionary *item in before) {
        if ([removed containsObject:item[@"attachment_uuid"]]) {
            for (NSDictionary *repair in replacements) if ([repair[@"before_uuid"] isEqual:item[@"attachment_uuid"]]) [expectedHashes addObject:repair[@"sha256"]];
            continue;
        }
        [expectedHashes addObject:item[@"sha256"]];
        NSDictionary *same=nil;
        for (NSDictionary *candidate in after) if ([candidate[@"attachment_uuid"] isEqual:item[@"attachment_uuid"]]) same=candidate;
        if (!same || ![same[@"sha256"] isEqual:item[@"sha256"]] || ![same[@"file_name"] isEqual:item[@"file_name"]]
            || ([item[@"remote_available"] boolValue] && ![same[@"remote_available"] boolValue])) return NO;
    }
    NSMutableSet *afterIDs=[NSMutableSet set];
    for (NSDictionary *item in after) {
        NSString *identifier=item[@"attachment_uuid"];
        if (!identifier.length || [removed containsObject:identifier] || [afterIDs containsObject:identifier]) return NO;
        [afterIDs addObject:identifier];[actualHashes addObject:item[@"sha256"]];
    }
    NSCountedSet *expected=[NSCountedSet setWithArray:expectedHashes], *actual=[NSCountedSet setWithArray:actualHashes];
    if (![expected isEqualToSet:actual]) return NO;
    for (id hash in expected) if ([expected countForObject:hash]!=[actual countForObject:hash]) return NO;
    return YES;
}

static NSArray *AlarmSnapshot(EKEvent *event) {
    NSMutableArray *values = [NSMutableArray array];
    for (EKAlarm *alarm in event.alarms ?: @[]) {
        [values addObject:@{@"relative_minutes":@(alarm.relativeOffset/60.0),
                            @"absolute_timestamp":alarm.absoluteDate ? @(alarm.absoluteDate.timeIntervalSince1970) : [NSNull null],
                            @"type":@(alarm.type)}];
    }
    return values;
}

static BOOL ContainsAlarm(NSArray *alarms, NSNumber *minutes) {
    for (NSDictionary *alarm in alarms) {
        if (alarm[@"absolute_timestamp"] == [NSNull null] && fabs([alarm[@"relative_minutes"] doubleValue]-minutes.doubleValue)<0.001) return YES;
    }
    return NO;
}

static EKEvent *ReviewEvent(EKEventStore *store, EKCalendar *calendar, NSDictionary *plan, BOOL exactUID, NSString **failure) {
    NSDate *start = [NSDate dateWithTimeIntervalSince1970:[plan[@"start_timestamp"] doubleValue]];
    NSString *marker = [@"法院材料事项ID：" stringByAppendingString:plan[@"event_key"] ?: @""];
    NSPredicate *pred = [store predicateForEventsWithStartDate:[start dateByAddingTimeInterval:-86400]
                                                      endDate:[start dateByAddingTimeInterval:86400] calendars:@[calendar]];
    NSMutableArray *found = [NSMutableArray array];
    for (EKEvent *event in [store eventsMatchingPredicate:pred]) {
        BOOL uidMatches=[event.calendarItemExternalIdentifier isEqual:plan[@"uid"]] || [event.calendarItemIdentifier isEqual:plan[@"uid"]] || [event.eventIdentifier isEqual:plan[@"uid"]];
        if ((exactUID ? uidMatches : [[event.notes componentsSeparatedByString:@"\n"] containsObject:marker])
            && fabs([event.startDate timeIntervalSinceDate:start])<1) [found addObject:event];
    }
    if (found.count != 1) { *failure=@"事项标识及时间未唯一匹配"; return nil; }
    EKEvent *event = found.firstObject;
    double endDelta = fabs(event.endDate.timeIntervalSince1970-[plan[@"end_timestamp"] doubleValue]);
    if (![event.title isEqualToString:plan[@"title"]]
        || ![(event.notes ?: @"") isEqualToString:plan[@"notes"]]
        || ![(event.location ?: @"") isEqualToString:plan[@"location"] ?: @""]
        || event.allDay != [plan[@"all_day"] boolValue]
        || endDelta > ([plan[@"all_day"] boolValue] ? 1.001 : 0.001)) {
        *failure=@"附件目标与日历回读内容不一致"; return nil;
    }
    // Calendar scripting's UID normally equals EventKit's external identifier.
    // The exact unique marker plus full metadata is checked as well, for stores
    // whose identifier mapping differs; never match just a title or case suffix.
    NSString *uid=plan[@"uid"];
    if (event.calendarItemExternalIdentifier.length && uid.length
        && ![event.calendarItemExternalIdentifier isEqualToString:uid]
        && ![event.calendarItemIdentifier isEqualToString:uid]
        && ![event.eventIdentifier isEqualToString:uid]) {
        *failure=@"日历 UID 与附件目标不一致"; return nil;
    }
    return event;
}

static int CourtReviewSync(const char *fileArgument, BOOL inspectOnly, BOOL repairLocalUpload) {
    @autoreleasepool {
        NSError *error=nil;
        NSData *data=[NSData dataWithContentsOfFile:[NSString stringWithUTF8String:fileArgument]];
        NSDictionary *plan=data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
        if (![plan isKindOfClass:[NSDictionary class]] || ![plan[@"uid"] length] || (!repairLocalUpload && !inspectOnly && ![plan[@"event_key"] length])) {
            fprintf(stderr,"无效复核计划\n"); return 64;
        }
        NSArray *paths=plan[@"attachment_paths"];
        if (![paths isKindOfClass:[NSArray class]] || (!inspectOnly && paths.count==0)) {
            fprintf(stderr,"缺少日期依据 PDF\n"); return 66;
        }
        NSMutableArray *sources=[NSMutableArray array];
        for (NSString *path in paths) {
            if (![path isKindOfClass:[NSString class]]) { fprintf(stderr,"附件路径无效\n"); return 66; }
            NSData *source=[NSData dataWithContentsOfFile:path];
            const char *bytes=source.bytes;
            if (![path isKindOfClass:[NSString class]] || ![path.pathExtension.lowercaseString isEqualToString:@"pdf"]
                || source.length<5 || memcmp(bytes,"%PDF-",5)!=0) {
                fprintf(stderr,"日期依据不是可读取 PDF\n"); return 66;
            }
            [sources addObject:@{@"source_path":path,@"sha256":SHA256Data(source),@"file_name":path.lastPathComponent}];
        }
        EKEventStore *store=[[EKEventStore alloc] init];
        if (!EnsureCalendarAccess(store,&error)) { fprintf(stderr,"日历附件权限不可用\n"); return 77; }
        EKCalendar *calendar=CalendarNamed(store,plan[@"calendar"] ?: @"法院工作日历");
        if (!calendar) { fprintf(stderr,"找不到工作日历\n"); return 69; }
        NSString *failure=nil;
        BOOL exactUID=repairLocalUpload || (inspectOnly && [plan[@"exact_uid_inspection"] boolValue]);
        EKEvent *event=ReviewEvent(store,calendar,plan,exactUID,&failure);
        if (!event) { fprintf(stderr,"%s\n",failure.UTF8String); return 70; }
        NSArray *beforeAlarms=AlarmSnapshot(event);
        NSArray<EKAlarm *> *originalAlarms=[event.alarms copy];
        BOOL changed=NO;
        if (!ReviewAttachments(event)) { fprintf(stderr,"当前系统不支持附件核验\n"); return 71; }
        NSArray *beforeInventory=AttachmentInventory(event), *repairs=nil;
        if (!inspectOnly && !repairLocalUpload) for (NSDictionary *source in sources) {
            if (HasUnreadableSameNameAttachment(source,beforeInventory)) {
                fprintf(stderr,"已有同名附件内容暂不可核验，禁止重复添加\n");return 80;
            }
        }
        if (repairLocalUpload) {
            repairs=PrepareAttachmentRepairs(event,plan,sources,&failure);
            if (!repairs) { fprintf(stderr,"%s\n",failure.UTF8String);return 78; }
            for (NSDictionary *repair in repairs) {
                SendVoid(event,@"removeAttachment:",repair[@"old"]);
                SendVoid(event,@"addAttachment:",repair[@"new"]);
            }
            changed=YES;
        }
        for (NSDictionary *source in sources) {
            BOOL found=NO;
            for (id attachment in ReviewAttachments(event)) {
                if ([AttachmentSHA(attachment) isEqualToString:source[@"sha256"]]) { found=YES; break; }
            }
            if (!found && !inspectOnly && !repairLocalUpload) {
                Class cls=NSClassFromString(@"EKAttachment");
                if (!cls || ![cls instancesRespondToSelector:NSSelectorFromString(@"initWithFilepath:")]
                    || ![event respondsToSelector:NSSelectorFromString(@"addAttachment:")]) {
                    fprintf(stderr,"当前系统不支持添加附件\n"); return 71;
                }
                id attachment=SendId([cls alloc],@"initWithFilepath:",[NSURL fileURLWithPath:source[@"source_path"]]);
                if (!attachment) { fprintf(stderr,"PDF附件创建失败\n"); return 71; }
                SendVoid(event,@"addAttachment:",attachment); changed=YES;
            }
        }
        NSNumber *wantedAlarm=plan[@"alarm_minutes"];
        if (wantedAlarm && wantedAlarm != (id)[NSNull null] && !ContainsAlarm(beforeAlarms,wantedAlarm) && !inspectOnly && !repairLocalUpload) {
            [event addAlarm:[EKAlarm alarmWithRelativeOffset:wantedAlarm.doubleValue*60.0]]; changed=YES;
        }
        if (changed && ![store saveEvent:event span:EKSpanThisEvent commit:YES error:&error]) {
            fprintf(stderr,"附件或提醒保存失败: %s\n",error.localizedDescription.UTF8String); return 72;
        }
        if (repairLocalUpload) fprintf(stderr,"指定附件已单次保存替换；后续回读若失败，仅重新检查，勿自动重复替换\n");
        // Refetch through a new store; a setter or the saved object's cache is not proof.
        __block EKEventStore *fresh=nil;
        __block EKCalendar *freshCalendar=nil;
        __block EKEvent *saved=nil;
        __block NSArray *verified=nil;
        __block NSString *syncScope=nil;
        int readback=BoundedAttachmentReadback(^int {
            fresh=[[EKEventStore alloc] init];
            freshCalendar=CalendarNamed(fresh,plan[@"calendar"] ?: @"法院工作日历");
            NSString *readFailure=nil;
            saved=freshCalendar ? ReviewEvent(fresh,freshCalendar,plan,exactUID,&readFailure) : nil;
            if (!saved) return 73;
            syncScope=AttachmentSyncScope(freshCalendar);
            verified=VerifiedReviewAttachments(saved,sources,syncScope);
            return verified ? 0 : 75;
        }, ^(NSTimeInterval seconds) { [NSThread sleepForTimeInterval:seconds]; });
        if (readback==73) { fprintf(stderr,"保存后无法重新读取唯一事件\n"); return 73; }
        if (readback) { fprintf(stderr,"未从日历读回内容一致的PDF附件；已有限只读重查，禁止盲目重复添加\n"); return readback; }
        NSArray *afterAlarms=AlarmSnapshot(saved);
        for (NSDictionary *alarm in beforeAlarms) {
            if (![afterAlarms containsObject:alarm]) {
                BOOL restored=NO;
                if (!inspectOnly && !repairLocalUpload) { saved.alarms=originalAlarms;restored=[fresh saveEvent:saved span:EKSpanThisEvent commit:YES error:&error]; }
                fprintf(stderr,"既有提醒未保留，恢复原提醒%s\n",restored ? "已提交" : "失败，需核对"); return 74;
            }
        }
        NSArray *afterInventory=AttachmentInventory(saved);
        if (!repairLocalUpload && !ExistingAttachmentsPreserved(beforeInventory,afterInventory)) {
            fprintf(stderr,"既有附件完整性未通过回读，禁止继续写入\n");return 79;
        }
        if (repairLocalUpload && (!VerifyAttachmentRepairs(beforeInventory,afterInventory,repairs) || ![beforeAlarms isEqual:afterAlarms])) {
            fprintf(stderr,"替换后附件或原提醒完整性未通过回读，禁止自动重复修复\n");return 79;
        }
        if (wantedAlarm && wantedAlarm != (id)[NSNull null] && !ContainsAlarm(afterAlarms,wantedAlarm)) {
            fprintf(stderr,"未读回要求的提前提醒\n"); return 74;
        }
        NSMutableDictionary *result=[@{@"uid":plan[@"uid"],@"event_key":plan[@"event_key"] ?: @"",@"attachments":verified,
                                @"alarms":afterAlarms,@"extras_verified":@YES,@"metadata_preserved":@YES,
                                @"attachment_inventory":afterInventory,@"calendar_item_identifier":saved.calendarItemIdentifier ?: @""} mutableCopy];
        AddAttachmentSyncStatus(result,syncScope,afterInventory);
        if (repairLocalUpload) {
            result[@"repair_saved"]=@YES;result[@"repair_before_inventory"]=beforeInventory;
            result[@"repair_after_inventory"]=afterInventory;
        }
        NSData *output=[NSJSONSerialization dataWithJSONObject:result options:0 error:&error];
        printf("%s\n",[[[NSString alloc] initWithData:output encoding:NSUTF8StringEncoding] UTF8String]);
        return 0;
    }
}

int main(int argc, const char *argv[]) {
    if (argc == 3 && strcmp(argv[1], "review-sync") == 0) return CourtReviewSync(argv[2], NO, NO);
    if (argc == 3 && strcmp(argv[1], "review-inspect") == 0) return CourtReviewSync(argv[2], YES, NO);
    if (argc == 3 && strcmp(argv[1], "repair-local-upload") == 0) return CourtReviewSync(argv[2], NO, YES);
    fprintf(stderr, "用法: court_event_attachment review-sync|review-inspect|repair-local-upload 计划JSON\n");
    return 64;
}
