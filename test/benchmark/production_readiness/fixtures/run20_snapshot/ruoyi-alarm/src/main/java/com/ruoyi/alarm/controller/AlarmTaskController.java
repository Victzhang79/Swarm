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
import com.ruoyi.alarm.domain.AlarmTask;
import com.ruoyi.alarm.domain.dto.AlarmTaskTestDTO;
import com.ruoyi.alarm.service.IAlarmTaskService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.core.text.Convert;
import com.ruoyi.common.enums.BusinessType;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.poi.ExcelUtil;

/**
 * 预警任务Controller
 * 
 * @author ruoyi
 */
@Controller
@RequestMapping("/alarm/task")
public class AlarmTaskController extends BaseController
{
    private String prefix = "alarm/task";

    @Autowired
    private IAlarmTaskService alarmTaskService;

    /**
     * 预警任务列表页面
     */
    @RequiresPermissions("alarm:task:view")
    @GetMapping()
    public String task()
    {
        return prefix + "/task";
    }

    /**
     * 查询预警任务列表
     */
    @RequiresPermissions("alarm:task:list")
    @PostMapping("/list")
    @ResponseBody
    public TableDataInfo list(AlarmTask alarmTask)
    {
        startPage();
        List<AlarmTask> list = alarmTaskService.selectAlarmTaskList(alarmTask);
        return getDataTable(list);
    }

    /**
     * 导出预警任务
     */
    @RequiresPermissions("alarm:task:export")
    @Log(title = "预警任务", businessType = BusinessType.EXPORT)
    @PostMapping("/export")
    @ResponseBody
    public AjaxResult export(AlarmTask alarmTask)
    {
        List<AlarmTask> list = alarmTaskService.selectAlarmTaskList(alarmTask);
        ExcelUtil<AlarmTask> util = new ExcelUtil<>(AlarmTask.class);
        return util.exportExcel(list, "预警任务数据");
    }

    /**
     * 新增预警任务
     */
    @RequiresPermissions("alarm:task:add")
    @GetMapping("/add")
    public String add(ModelMap mmap)
    {
        return prefix + "/add";
    }

    /**
     * 新增保存预警任务
     */
    @RequiresPermissions("alarm:task:add")
    @Log(title = "预警任务", businessType = BusinessType.INSERT)
    @PostMapping("/add")
    @ResponseBody
    public AjaxResult addSave(@Validated AlarmTask alarmTask)
    {
        if (StringUtils.isEmpty(alarmTask.getTaskName()))
        {
            return error("任务名称不能为空");
        }
        if (alarmTaskService.selectAlarmTaskByName(alarmTask.getTaskName()) != null)
        {
            return error("任务名称已存在");
        }
        return toAjax(alarmTaskService.insertAlarmTask(alarmTask));
    }

    /**
     * 修改预警任务
     */
    @RequiresPermissions("alarm:task:edit")
    @GetMapping("/edit/{taskId}")
    public String edit(@PathVariable("taskId") Long taskId, ModelMap mmap)
    {
        AlarmTask alarmTask = alarmTaskService.selectAlarmTaskById(taskId);
        if (alarmTask == null)
        {
            return redirect("/alarm/task");
        }
        mmap.put("alarmTask", alarmTask);
        return prefix + "/edit";
    }

    /**
     * 修改保存预警任务
     */
    @RequiresPermissions("alarm:task:edit")
    @Log(title = "预警任务", businessType = BusinessType.UPDATE)
    @PostMapping("/edit")
    @ResponseBody
    public AjaxResult editSave(@Validated AlarmTask alarmTask)
    {
        if (StringUtils.isEmpty(alarmTask.getTaskName()))
        {
            return error("任务名称不能为空");
        }
        AlarmTask existing = alarmTaskService.selectAlarmTaskByName(alarmTask.getTaskName());
        if (existing != null && !existing.getTaskId().equals(alarmTask.getTaskId()))
        {
            return error("任务名称已存在");
        }
        return toAjax(alarmTaskService.updateAlarmTask(alarmTask));
    }

    /**
     * 删除预警任务
     */
    @RequiresPermissions("alarm:task:remove")
    @Log(title = "预警任务", businessType = BusinessType.DELETE)
    @PostMapping("/remove")
    @ResponseBody
    public AjaxResult remove(String ids)
    {
        return toAjax(alarmTaskService.deleteAlarmTaskByIds(Convert.toLongArray(ids)));
    }

    /**
     * 测试发送预警
     */
    @RequiresPermissions("alarm:task:test")
    @Log(title = "预警任务", businessType = BusinessType.OTHER)
    @PostMapping("/testSend")
    @ResponseBody
    public AjaxResult testSend(@Validated AlarmTaskTestDTO testDTO)
    {
        boolean result = alarmTaskService.testSend(testDTO);
        return result ? success("测试发送成功") : error("测试发送失败");
    }

    /**
     * 校验任务名称是否唯一
     */
    @PostMapping("/checkTaskNameUnique")
    @ResponseBody
    public boolean checkTaskNameUnique(AlarmTask alarmTask)
    {
        AlarmTask existing = alarmTaskService.selectAlarmTaskByName(alarmTask.getTaskName());
        return existing == null;
    }
}
