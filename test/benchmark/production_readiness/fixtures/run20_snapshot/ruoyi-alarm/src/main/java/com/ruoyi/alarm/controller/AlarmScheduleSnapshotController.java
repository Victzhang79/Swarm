package com.ruoyi.alarm.controller;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import com.ruoyi.alarm.domain.AlarmScheduleSnapshot;
import com.ruoyi.alarm.service.IAlarmScheduleSnapshotService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.enums.BusinessType;

/**
 * 排班快照Controller
 * 
 * @author ruoyi
 */
@RestController
@RequestMapping("/alarm/schedule/snapshot")
public class AlarmScheduleSnapshotController extends BaseController
{
    private String prefix = "alarm/schedule/snapshot";

    @Autowired
    private IAlarmScheduleSnapshotService snapshotService;

    /**
     * 查询排班快照列表
     */
    @RequiresPermissions("alarm:schedule:snapshot:list")
    @GetMapping("/list")
    public TableDataInfo list(AlarmScheduleSnapshot snapshot)
    {
        startPage();
        List<AlarmScheduleSnapshot> list = snapshotService.selectSnapshotList(snapshot);
        return getDataTable(list);
    }

    /**
     * 获取排班快照详情
     */
    @RequiresPermissions("alarm:schedule:snapshot:query")
    @GetMapping("/detail/{snapshotId}")
    public AjaxResult detail(@PathVariable("snapshotId") Long snapshotId)
    {
        AlarmScheduleSnapshot snapshot = snapshotService.selectSnapshotById(snapshotId);
        return AjaxResult.success(snapshot);
    }

    /**
     * 人工覆盖排班
     */
    @RequiresPermissions("alarm:schedule:snapshot:edit")
    @Log(title = "排班快照", businessType = BusinessType.UPDATE)
    @PostMapping("/override")
    public AjaxResult override(AlarmScheduleSnapshot snapshot)
    {
        int result = snapshotService.manualOverride(snapshot.getSnapshotId(), snapshot.getGroupId(), snapshot.getMemberIds());
        return toAjax(result);
    }

    /**
     * 手动触发通知发送
     */
    @RequiresPermissions("alarm:schedule:snapshot:edit")
    @Log(title = "排班快照", businessType = BusinessType.UPDATE)
    @PostMapping("/notify")
    public AjaxResult notify(@RequestParam("snapshotId") Long snapshotId)
    {
        int result = snapshotService.sendNotification(snapshotId);
        return toAjax(result);
    }
}
