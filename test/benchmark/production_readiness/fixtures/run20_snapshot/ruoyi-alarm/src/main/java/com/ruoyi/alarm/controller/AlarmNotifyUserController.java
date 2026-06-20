package com.ruoyi.alarm.controller;

import java.util.List;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.ModelMap;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseBody;
import com.ruoyi.alarm.domain.AlarmNotifyUser;
import com.ruoyi.alarm.service.IAlarmNotifyUserService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.core.text.Convert;
import com.ruoyi.common.enums.BusinessType;
import com.ruoyi.common.utils.poi.ExcelUtil;

/**
 * 通知用户 Controller
 *
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/notifyuser")
public class AlarmNotifyUserController extends BaseController
{
    private String prefix = "alarm/notifyuser";

    @Autowired
    private IAlarmNotifyUserService alarmNotifyUserService;

    @RequiresPermissions("alarm:notifyuser:view")
    @GetMapping()
    public String notifyuser()
    {
        return prefix + "/list";
    }

    @RequiresPermissions("alarm:notifyuser:list")
    @PostMapping("/list")
    @ResponseBody
    public TableDataInfo list(AlarmNotifyUser alarmNotifyUser)
    {
        startPage();
        List<AlarmNotifyUser> list = alarmNotifyUserService.selectAlarmNotifyUserList(alarmNotifyUser);
        return getDataTable(list);
    }

    @Log(title = "通知用户", businessType = BusinessType.EXPORT)
    @RequiresPermissions("alarm:notifyuser:export")
    @PostMapping("/export")
    @ResponseBody
    public AjaxResult export(AlarmNotifyUser alarmNotifyUser)
    {
        List<AlarmNotifyUser> list = alarmNotifyUserService.selectAlarmNotifyUserList(alarmNotifyUser);
        ExcelUtil<AlarmNotifyUser> util = new ExcelUtil<>(AlarmNotifyUser.class);
        return util.exportExcel(list, "通知用户数据");
    }

    @RequiresPermissions("alarm:notifyuser:view")
    @GetMapping("/importTemplate")
    @ResponseBody
    public AjaxResult importTemplate()
    {
        ExcelUtil<AlarmNotifyUser> util = new ExcelUtil<>(AlarmNotifyUser.class);
        return util.importTemplateExcel("通知用户数据");
    }

    /**
     * 新增通知用户
     */
    @RequiresPermissions("alarm:notifyuser:add")
    @GetMapping("/add")
    public String add(ModelMap mmap)
    {
        return prefix + "/add";
    }

    /**
     * 新增保存通知用户
     */
    @RequiresPermissions("alarm:notifyuser:add")
    @Log(title = "通知用户", businessType = BusinessType.INSERT)
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult addSave(AlarmNotifyUser alarmNotifyUser)
    {
        alarmNotifyUser.setCreateBy(getLoginName());
        return toAjax(alarmNotifyUserService.insertAlarmNotifyUser(alarmNotifyUser));
    }

    /**
     * 修改通知用户
     */
    @RequiresPermissions("alarm:notifyuser:edit")
    @GetMapping("/edit/{userId}")
    public String edit(@PathVariable("userId") Long userId, ModelMap mmap)
    {
        AlarmNotifyUser alarmNotifyUser = alarmNotifyUserService.selectAlarmNotifyUserById(userId);
        mmap.put("alarmNotifyUser", alarmNotifyUser);
        return prefix + "/edit";
    }

    /**
     * 修改保存通知用户
     */
    @RequiresPermissions("alarm:notifyuser:edit")
    @Log(title = "通知用户", businessType = BusinessType.UPDATE)
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult editSave(AlarmNotifyUser alarmNotifyUser)
    {
        alarmNotifyUser.setUpdateBy(getLoginName());
        return toAjax(alarmNotifyUserService.updateAlarmNotifyUser(alarmNotifyUser));
    }

    /**
     * 删除通知用户
     */
    @RequiresPermissions("alarm:notifyuser:remove")
    @Log(title = "通知用户", businessType = BusinessType.DELETE)
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(String userIds)
    {
        return toAjax(alarmNotifyUserService.deleteAlarmNotifyUserByIds(Convert.toLongArray(userIds)));
    }
}
