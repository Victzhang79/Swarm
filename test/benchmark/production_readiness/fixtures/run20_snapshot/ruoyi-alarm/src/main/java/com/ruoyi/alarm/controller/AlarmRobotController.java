package com.ruoyi.alarm.controller;

import java.util.List;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.ModelMap;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseBody;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.enums.BusinessType;
import com.ruoyi.common.utils.poi.ExcelUtil;
import com.ruoyi.alarm.domain.AlarmRobot;
import com.ruoyi.alarm.service.IAlarmRobotService;

/**
 * 机器人管理 Controller
 *
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/robot")
public class AlarmRobotController extends BaseController
{
    private String prefix = "alarm/robot";

    @Autowired
    private IAlarmRobotService alarmRobotService;

    /**
     * 机器人列表页
     */
    @RequiresPermissions("alarm:robot:list")
    @GetMapping()
    public String list(ModelMap mmap)
    {
        return prefix + "/list";
    }

    /**
     * 查询机器人列表
     */
    @RequiresPermissions("alarm:robot:list")
    @PostMapping("/list")
    @ResponseBody
    public TableDataInfo list(AlarmRobot robot)
    {
        startPage();
        List<AlarmRobot> list = alarmRobotService.selectAlarmRobotList(robot);
        return getDataTable(list);
    }

    /**
     * 导出机器人
     */
    @Log(title = "机器人", businessType = BusinessType.EXPORT)
    @RequiresPermissions("alarm:robot:export")
    @PostMapping("/export")
    @ResponseBody
    public AjaxResult export(AlarmRobot robot)
    {
        List<AlarmRobot> list = alarmRobotService.selectAlarmRobotList(robot);
        ExcelUtil<AlarmRobot> util = new ExcelUtil<AlarmRobot>(AlarmRobot.class);
        return util.exportExcel(list, "机器人");
    }

    /**
     * 新增机器人
     */
    @RequiresPermissions("alarm:robot:add")
    @GetMapping("/add")
    public String add()
    {
        return prefix + "/add";
    }

    /**
     * 新增保存机器人
     */
    @Log(title = "机器人", businessType = BusinessType.INSERT)
    @RequiresPermissions("alarm:robot:add")
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult addSave(@Validated AlarmRobot robot)
    {
        robot.setCreateBy(getLoginName());
        if (robot.getStatus() == null || robot.getStatus().isEmpty())
        {
            robot.setStatus("0");
        }
        return toAjax(alarmRobotService.insertAlarmRobot(robot));
    }

    /**
     * 修改机器人
     */
    @RequiresPermissions("alarm:robot:edit")
    @GetMapping("/edit/{robotId}")
    public String edit(@PathVariable("robotId") Long robotId, ModelMap mmap)
    {
        mmap.put("robot", alarmRobotService.selectAlarmRobotById(robotId));
        return prefix + "/edit";
    }

    /**
     * 修改保存机器人
     */
    @Log(title = "机器人", businessType = BusinessType.UPDATE)
    @RequiresPermissions("alarm:robot:edit")
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult editSave(@Validated AlarmRobot robot)
    {
        robot.setUpdateBy(getLoginName());
        return toAjax(alarmRobotService.updateAlarmRobot(robot));
    }

    /**
     * 删除机器人
     */
    @Log(title = "机器人", businessType = BusinessType.DELETE)
    @RequiresPermissions("alarm:robot:remove")
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(String ids)
    {
        alarmRobotService.deleteAlarmRobotByIds(ids);
        return success();
    }

    /**
     * 根据ID查询机器人详情
     */
    @RequiresPermissions("alarm:robot:list")
    @GetMapping("/detail/{robotId}")
    @ResponseBody
    public AjaxResult detail(@PathVariable("robotId") Long robotId)
    {
        return success(alarmRobotService.selectAlarmRobotById(robotId));
    }
}
